import copy
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App


class AppIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App({
            'aiostreams_url': 'http://aio.invalid:3000',
            'active_uuid': 'family', 'active_password': 'password',
            'stremio_uuid': 'family', 'stremio_encrypted_password': 'encrypted',
            'catalog_uuid': 'original', 'catalog_encrypted_password': 'catalog-encrypted',
            'endpoint_token': 'test-token-with-at-least-24-characters',
            'max_episodes_per_series': 2, 'stream_requests_per_minute': 30,
        }, self.directory.name + '/db')
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.app.store.db.close)

    def add_title(self, kind):
        self.app.store.add_categories([{'id': 'original.shelf', 'name': 'Shelf', 'type': kind}], 'cachedlibrary')
        category = self.app.store.db.execute('SELECT * FROM categories').fetchone()
        self.app.store.add_page(category, [{'id': 'tt1', 'name': 'One'}], 0, 100)

    def test_filtered_profile_cannot_be_its_own_metadata_source(self):
        self.assertEqual(self.app.config_problems(), [])
        self.app.o['catalog_uuid'] = 'family'
        self.assertIn('Catalogue metadata must not use the filtered playback profile', self.app.config_problems())

    def test_metadata_worker_requests_original_profile(self):
        self.add_title('series')
        self.app.ready = True
        calls = []

        def request(path, **kwargs):
            calls.append((path, kwargs))
            self.app.stop.set()
            return {'meta': {'id': 'tt1', 'name': 'One', 'type': 'series', 'videos': [
                {'id': 'tt1:1:1', 'season': 1, 'episode': 1, 'released': '2000-01-01T00:00:00Z'},
                {'id': 'tt1:1:2', 'season': 1, 'episode': 2, 'released': '2000-01-01T00:00:00Z'},
                {'id': 'tt1:1:3', 'season': 1, 'episode': 3, 'released': '2000-01-01T00:00:00Z'},
            ]}}

        self.app.request = request
        self.app.metadata_worker(0)
        self.assertEqual(calls, [('/meta/series/tt1.json', {'catalog': True})])
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 3)

    def test_failed_worker_releases_claim_and_preserves_positive(self):
        self.add_title('movie')
        self.app.ready = True
        self.app.store.record('movie', 'tt1', 'tt1', 'available', 1, time.time())
        self.app.store.db.execute('UPDATE checks SET due=0')
        self.app.store.db.commit()
        expiry = self.app.store.db.execute('SELECT expires FROM checks').fetchone()[0]

        def request(*args, **kwargs):
            self.app.stop.set()
            raise RuntimeError('provider failed')

        self.app.request = request
        self.app.worker(0)
        row = self.app.store.db.execute('SELECT * FROM checks').fetchone()
        self.assertEqual(row['status'], 'available')
        self.assertEqual(row['expires'], expiry)
        self.assertEqual(row['failures'], 1)
        self.assertFalse(self.app.inflight)

    def test_result_started_under_old_policy_is_discarded(self):
        self.add_title('movie')
        self.app.ready = True
        self.app.store.setting('policy', 'old')

        def request(*args, **kwargs):
            self.app.store.setting('policy', 'new')
            self.app.stop.set()
            return {'streams': [{'url': 'https://cdn.invalid/real.mkv'}]}

        self.app.request = request
        self.app.worker(0)
        row = self.app.store.db.execute('SELECT * FROM checks').fetchone()
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['checked'], 0)
        self.assertFalse(self.app.inflight)

    def test_synchronization_keeps_verdicts_when_only_display_changes(self):
        config = {'excludeUncached': True, 'requiredStreamExpressions': [
            {'enabled': True, 'expression': "cached(service(streams, 'torbox'))"}],
            'presets': [{'type': 'torrentio', 'instanceId': 'provider', 'enabled': True,
                         'options': {'resources': ['stream']}}]}

        def request(path, config=False, catalog=False):
            if config:
                return {'data': {'userData': current}}
            self.assertTrue(catalog)
            return {'catalogs': [{'id': 'original.shelf', 'name': 'Shelf', 'type': 'movie'}]}

        current = copy.deepcopy(config)
        self.app.request = request
        self.app.synchronize()
        self.add_title('movie')
        self.app.store.record('movie', 'tt1', 'tt1', 'available', 1, time.time())
        current.update(formatter={'id': 'custom', 'name': 'Short'}, sortCriteria={'global': []})
        current['presets'].append({'type': 'custom', 'enabled': True, 'instanceId': 'cachedlibrary',
                                   'options': {'resources': ['meta', 'catalog']}})
        self.app.synchronize()
        self.assertTrue(self.app.store.visible('movie', 'tt1', time.time()))
        current['requiredLanguages'] = ['Russian']
        self.app.synchronize()
        self.assertFalse(self.app.store.visible('movie', 'tt1', time.time()))

    def test_global_backoff_and_pacing_share_one_clock(self):
        class FakeStop:
            def is_set(self):
                return False

            def wait(self, seconds):
                clock[0] += seconds

        clock = [100.0]
        self.app.stop = FakeStop()
        with patch('server.time.monotonic', side_effect=lambda: clock[0]):
            self.app.pace_stream()
            self.app.pace_stream()
            self.assertEqual(clock[0], 102.0)
            self.app.backoff('60')
            self.app.pace_stream()
            self.assertEqual(clock[0], 162.0)
            self.app.pace_stream()
            self.assertEqual(clock[0], 164.0)

    def test_guard_restarts_a_failed_worker(self):
        calls = []

        def worker():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('transient')
            self.app.stop.set()

        with patch.object(self.app.stop, 'wait', return_value=False), patch('builtins.print'):
            self.app.guarded('worker0', worker)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.app.worker_errors['worker0']['type'], 'RuntimeError')


if __name__ == '__main__':
    unittest.main()
