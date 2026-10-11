import os
import sys
import threading
import time
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from search_jobs import SearchJobs
from server import App


class SearchJobsTests(unittest.TestCase):
    def test_duplicate_requests_share_one_bounded_job(self):
        discover = Mock(return_value=['tt1'])
        jobs = SearchJobs(discover, threading.Event(), max_pending=1)
        first = jobs.submit('movie', ' Puss  in Boots ')
        self.assertIs(jobs.submit('movie', 'puss in boots'), first)
        self.assertIsNone(jobs.submit('movie', 'Another movie'))
        jobs.run_one(timeout=0)
        self.assertTrue(first['done'].is_set())
        self.assertEqual(first['ids'], ['tt1'])
        self.assertIs(jobs.submit('movie', 'Puss in Boots'), first)
        discover.assert_called_once_with('movie', 'Puss in Boots')

    def test_failure_is_retryable_and_does_not_become_empty_success(self):
        jobs = SearchJobs(Mock(side_effect=RuntimeError('private URL')), threading.Event())
        job = jobs.submit('series', 'Bluey')
        before = time.monotonic()
        jobs.run_one(timeout=0)
        self.assertTrue(job['error'])
        self.assertLess(job['expires'], before + 31)
        with patch('search_jobs.time.monotonic', return_value=before + 32):
            self.assertIsNot(jobs.submit('series', 'Bluey'), job)

    def test_completed_query_history_has_a_limit(self):
        jobs = SearchJobs(lambda *args: [], threading.Event(), max_recent=2)
        for query in ['One', 'Two', 'Three']:
            self.assertIsNotNone(jobs.submit('movie', query))
            jobs.run_one(timeout=0)
        self.assertEqual(len(jobs.jobs), 2)

    def test_invalid_or_stopping_queries_do_not_queue_network_work(self):
        stop = threading.Event()
        jobs = SearchJobs(Mock(), stop)
        for kind, query in [('movie', ''), ('movie', 'x'), ('movie', '!!!'), ('movie', 'x'*201), ('music', 'Bluey')]:
            self.assertIsNone(jobs.submit(kind, query))
        stop.set()
        self.assertIsNone(jobs.submit('movie', 'Puss in Boots'))
        self.assertTrue(jobs.queue.empty())


class SearchAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App({'endpoint_token': 'test-token-with-24-characters'}, self.temp.name+'/db')
        self.app.ready = True
        self.app.SEARCH_WAIT_SECONDS = 0.02
        self.app.tmdb = Mock()
        self.app.tmdb.search.return_value = {'metas': [{'id': 'tt1234567', 'name': 'Puss in Boots'}]}

    def tearDown(self):
        self.app.stop.set()
        self.app.store.db.close()
        self.temp.cleanup()

    def test_remote_discovery_is_not_availability_evidence(self):
        ids = self.app.discover_search('movie', 'puss in the boots')
        self.assertEqual(ids, ['tt1234567'])
        self.assertEqual(self.app.store.search_matches('movie', 'puss in the boots'), ids)
        self.assertEqual(self.app.search_catalog('movie', 'puss in the boots'), [])
        self.assertIsNone(self.app.store.meta('movie', ids[0]))
        next_check = self.app.next_check('movie')
        self.assertEqual(next_check['id'], ids[0])
        self.app.store.record('movie', ids[0], ids[0], 'available', 2, time.time())
        self.assertEqual([i['id'] for i in self.app.search_catalog('movie', 'puss in the boots')], ids)
        self.assertEqual(self.app.manifest()['catalogs'][0]['id'], 'cached-search')

    def test_local_confirmed_results_work_during_discovery_failure(self):
        self.app.discover_search('movie', 'Puss in Boots')
        self.app.store.record('movie', 'tt1234567', 'tt1234567', 'available', 2, time.time())
        self.app.tmdb.search.side_effect = RuntimeError('offline')
        job = self.app.search_jobs.submit('movie', 'Puss in Boots')
        self.app.search_jobs.run_one(timeout=0)
        self.assertTrue(job['error'])
        self.assertEqual(len(self.app.search_catalog('movie', 'Puss in Boots')), 1)

    def test_policy_readiness_checked_after_remote_discovery(self):
        def changed(*args, **kwargs):
            self.app.ready = False
            return {'metas': [{'id': 'tt1234567', 'name': 'Puss in Boots'}]}
        self.app.tmdb.search.side_effect = changed
        with self.assertRaises(RuntimeError):
            self.app.discover_search('movie', 'Puss in Boots')
        self.assertEqual(self.app.store.search_matches('movie', 'Puss in Boots'), [])

    def test_policy_failure_cannot_publish_existing_results(self):
        self.app.discover_search('movie', 'Puss in Boots')
        self.app.store.record('movie', 'tt1234567', 'tt1234567', 'available', 2, time.time())
        self.app.ready = False
        self.assertEqual(self.app.search_catalog('movie', 'Puss in Boots'), [])

    def test_remote_discovery_worker_does_not_fetch_streams(self):
        self.app.request = Mock(side_effect=AssertionError('unexpected stream or metadata fetch'))
        job = self.app.search_jobs.submit('series', 'Puss in Boots')
        self.app.search_jobs.run_one(timeout=0)
        self.assertFalse(job['error'])
        self.app.request.assert_not_called()
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 0)
