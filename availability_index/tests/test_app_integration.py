"""Request routing, progressive scheduling and public API contract regressions."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App, Handler

PAST='2000-01-01T00:00:00Z'


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.options={'aiostreams_url':'http://aio.invalid','active_uuid':'active','active_password':'p',
                      'stremio_uuid':'active','stremio_encrypted_password':'e',
                      'catalog_uuid':'original','catalog_encrypted_password':'e',
                      'endpoint_token':'test-private-token-long-enough','max_episodes_per_series':2}
        self.app=App(self.options,self.temp.name+'/db')
        self.app.store.add_categories([{'type':'series','id':'shows','name':'Shows'}],'cachedlibrary')
        self.cat=self.app.store.db.execute('SELECT * FROM categories').fetchone()
        self.app.store.add_page(self.cat,[{'id':'tt1','name':'Show One'},{'id':'tt2','name':'Show Two'}],0,100)
        self.app.ready=True

    def tearDown(self):
        self.app.stop.set()
        self.app.store.db.close()
        self.temp.cleanup()

    def save(self,ident='tt1',count=5):
        self.app.store.save_meta('series',ident,{'id':ident,'name':ident,'genres':['Animation','Family'],
            'videos':[{'id':ident+':1:'+str(i),'season':1,'episode':i,'released':PAST} for i in range(1,count+1)]},time.time())

    def test_metadata_always_uses_original_profile(self):
        calls=[]
        def request(path,**kwargs):
            calls.append((path,kwargs))
            self.app.stop.set()
            return {'meta':{'id':'tt1','name':'Show One','videos':[{'id':'tt1:1:1','season':1,'episode':1,'released':PAST}]}}
        self.app.request=request
        self.app.metadata_worker(0)
        self.assertEqual(calls,[('/meta/series/tt1.json',{'catalog':True})])

    def test_batches_expand_past_old_episode_cap_and_rotate_shows(self):
        self.save('tt1'); self.save('tt2')
        picked=[]
        for _ in range(10):
            row=self.app.next_check('series')
            self.assertIsNotNone(row)
            picked.append((row['parent'],row['id']))
            self.app.store.record(row['type'],row['id'],row['parent'],'available',1,time.time())
            self.app.inflight.discard((row['type'],row['id'],row['parent']))
        self.assertEqual([p for p,_ in picked[:4]],['tt1','tt1','tt2','tt2'])
        self.assertEqual(len(set(picked)),10)
        self.assertIn(('tt1','tt1:1:5'),picked)
        self.assertIsNone(self.app.next_check('series'))

    def test_expiring_confirmation_precedes_new_backlog(self):
        self.save()
        self.app.store.record('series','tt1:1:5','tt1','available',1,time.time()-40000)
        row=self.app.next_check('series')
        self.assertEqual(row['id'],'tt1:1:5')

    def test_separate_catalogue_and_matching_stream_policy_required(self):
        self.assertEqual(self.app.config_problems(),[])
        self.app.o['catalog_uuid']='active'
        self.assertIn('Catalogue metadata must not use the filtered playback profile',self.app.config_problems())
        self.app.o['stremio_uuid']='unchecked'
        self.assertIn('The policy and stream profile UUIDs must match',self.app.config_problems())

    def test_shared_request_spacing_and_retry_after(self):
        clock=[100.0]
        waits=[]
        class Stop:
            def is_set(self):return False
            def wait(self,seconds):waits.append(seconds);clock[0]+=seconds
            def set(self):pass
        self.app.stop=Stop()
        with patch('server.time.monotonic',side_effect=lambda:clock[0]):
            self.app.pace_stream()
            self.app.pace_stream()
            self.assertAlmostEqual(sum(waits),2.0)
            self.app.backoff('45')
            self.app.pace_stream()
            self.assertAlmostEqual(clock[0],147.0)

    def test_http_only_publishes_confirmed_episodes_and_genres(self):
        self.save()
        self.app.store.record('series','tt1:1:2','tt1','available',1,time.time())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.app=self.app
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        root='http://127.0.0.1:'+str(server.server_port)+'/'+self.options['endpoint_token']
        try:
            with urlopen(root+'/manifest.json') as f:manifest=json.load(f)
            genre=next(e for e in manifest['catalogs'][0]['extra'] if e['name']=='genre')
            self.assertEqual(genre['options'],['Animation','Family'])
            with urlopen(root+'/meta/series/tt1.json') as f:meta=json.load(f)['meta']
            self.assertEqual([v['id'] for v in meta['videos']],['tt1:1:2'])
            with urlopen(root+'/catalog/series/cached-search/search=tt1.json') as f:items=json.load(f)['metas']
            self.assertEqual(len(items),1)
            self.app.store.invalidate()
            with urlopen(root+'/meta/series/tt1.json') as f:self.assertIsNone(json.load(f)['meta'])
            with urlopen(root+'/catalog/series/cached-search/search=tt1.json') as f:self.assertEqual(json.load(f)['metas'],[])
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_guard_restarts_failed_worker(self):
        calls=[]
        def work():
            calls.append(1)
            if len(calls)==1:raise RuntimeError('Secret must not be logged')
            self.app.stop.set()
        with patch.object(self.app.stop,'wait',return_value=False),patch('builtins.print') as log:
            self.app.guarded('test',work)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.app.worker_errors['test']['type'],'RuntimeError')
        self.assertNotIn('Secret',str(log.call_args_list))

    def test_hidden_partial_provider_failure_cannot_replace_positive(self):
        self.save()
        self.app.store.record('series','tt1:1:1','tt1','available',1,time.time()-40000)
        expiry=self.app.store.db.execute("SELECT expires FROM checks WHERE id='tt1:1:1'").fetchone()[0]
        def request(path,**kwargs):
            self.app.stop.set()
            return {'streams':[{'name':'Removal Reasons','description':'Excluded Uncached (8)'}]}
        self.app.request=request
        self.app.worker(0)
        row=self.app.store.db.execute("SELECT status,expires,failures FROM checks WHERE id='tt1:1:1'").fetchone()
        self.assertEqual((row['status'],row['expires'],row['failures']),('available',expiry,1))

    def test_client_interest_advances_checks_without_publishing_pending_results(self):
        self.save('tt1');self.save('tt2')
        self.app.prioritize('series',search='Show Two')
        self.assertEqual(self.app.store.catalog('series','cached-search',search='Show Two'),[])
        self.assertIsNone(self.app.store.meta('series','tt2'))
        row=self.app.next_check('series')
        self.assertEqual(row['parent'],'tt2')
        touched=self.app.store.db.execute("SELECT scan_touched FROM titles WHERE id='tt2'").fetchone()[0]
        self.app.prioritize('series',ident='tt2')
        self.assertEqual(self.app.store.db.execute("SELECT scan_touched FROM titles WHERE id='tt2'").fetchone()[0],touched)

    def test_interest_prioritizes_original_metadata_and_ignores_unknown_titles(self):
        self.app.prioritize('series',ident='unknown')
        self.assertEqual(self.app.interest_times,{})
        self.app.prioritize('series',ident='tt2')
        self.assertEqual(self.app.claim_meta()['id'],'tt2')

if __name__=='__main__':unittest.main()
