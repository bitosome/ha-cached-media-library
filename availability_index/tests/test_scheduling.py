"""Scheduling fairness between movies and series probes, and interactive requests."""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App({
            'aiostreams_url': 'http://aio.invalid', 'active_uuid': 'active',
            'active_password': 'p', 'stremio_uuid': 'active',
            'stremio_encrypted_password': 'e', 'catalog_uuid': 'source',
            'catalog_encrypted_password': 'e',
            'endpoint_token': 'not-a-real-secret-long-enough',
            'max_episodes_per_series': 3,
        }, self.temp.name + '/db')
        self.app.ready = True
        self.app.store.add_categories([
            {'id': 'movies', 'type': 'movie', 'name': 'Movies'},
            {'id': 'shows', 'type': 'series', 'name': 'Shows'},
        ], 'cachedlibrary')
        self.categories = {r['type']: r for r in self.app.store.db.execute('SELECT * FROM categories')}

    def tearDown(self):
        self.app.stop.set()
        self.app.store.db.close()
        self.temp.cleanup()

    def add_movies(self, count):
        self.app.store.add_page(self.categories['movie'], [
            {'id': 'movie%03d' % i, 'name': 'Movie %d' % i} for i in range(count)
        ], 0, 1000)

    def add_shows(self, count, episodes=12, seasons=2):
        # Two seasons give each show two probes: <show>:2:1, then <show>:1:1.
        self.app.store.add_page(self.categories['series'], [
            {'id': 'show%03d' % i, 'name': 'Show %d' % i} for i in range(count)
        ], 0, 1000)
        for i in range(count):
            ident = 'show%03d' % i
            self.app.store.save_meta('series', ident, {
                'id': ident, 'name': ident,
                'videos': [{'id': '%s:%d:%d' % (ident, season, e), 'season': season,
                            'episode': e, 'released': '2000-01-01T00:00:00Z'}
                           for season in range(1, seasons + 1) for e in range(1, episodes + 1)],
            }, time.time())

    def finish(self, row, verdict='error'):
        self.app.store.record(row['type'], row['id'], row['parent'], verdict,
                              1 if verdict == 'available' else 0, time.time())
        self.app.inflight.discard((row['type'], row['id'], row['parent']))

    def mark_show_verified(self, ident):
        self.app.store.record('series', ident + ':2:1', ident, 'available', 1, time.time())
        self.app.store.db.execute('UPDATE titles SET scan_touched=? WHERE type=? AND id=?',
                                  (time.time() - 10, 'series', ident))
        self.app.store.db.commit()

    def test_movie_turns_do_not_consume_pending_series_batch(self):
        self.add_movies(5)
        self.add_shows(4)
        self.app.prioritize('series', ident='show000')
        picked = []
        for i in range(8):
            preferred = 'series' if i % 2 == 0 else 'movie'
            row = self.app.next_check(preferred)
            self.assertIsNotNone(row)
            picked.append(row['type'])
            self.finish(row)
        self.assertEqual(picked, ['series', 'movie'] * 4)

    def test_worker_requests_alternate_types_despite_series_backlog(self):
        self.add_movies(5)
        self.add_shows(5)
        calls = []
        def request(path):
            calls.append(path.split('/')[2])
            if len(calls) == 8:
                self.app.stop.set()
            return {'streams': [{'url': 'https://example.invalid/video'}]}
        self.app.request = request
        with patch.object(self.app.stop, 'wait', return_value=False):
            self.app.worker(0)
        self.assertEqual(calls, ['movie', 'series'] * 4)

    def test_each_show_is_confirmed_by_one_check(self):
        self.add_shows(20, episodes=8)
        picked = []
        for _ in range(20):
            row = self.app.next_check('series')
            picked.append((row['parent'], row['id']))
            self.finish(row, 'available')
        self.assertEqual(picked, [('show%03d' % i, 'show%03d:2:1' % i) for i in range(20)])
        # Confirmed shows need nothing more until their renewal.
        self.assertIsNone(self.app.next_check('series'))

    def test_unverified_shows_continue_one_episode_per_rotation(self):
        self.add_shows(3)
        picked = []
        for _ in range(6):
            row = self.app.next_check('series')
            picked.append(row['id'])
            self.finish(row)
        # Newest-season openers in turn, then the first-season fallbacks.
        self.assertEqual(picked, ['show000:2:1', 'show001:2:1', 'show002:2:1',
                                  'show000:1:1', 'show001:1:1', 'show002:1:1'])
        self.assertIsNone(self.app.next_check('series'))

    def test_requested_show_gets_batch_without_stealing_movie_turn(self):
        self.add_movies(2)
        self.add_shows(3)
        self.app.prioritize('series', ident='show002')
        picked = []
        for _ in range(2):
            row = self.app.next_check('series')
            picked.append(row['id'])
            self.finish(row)
            if len(picked) == 1:
                movie = self.app.next_check('movie')
                self.assertEqual(movie['type'], 'movie')
                self.finish(movie)
        self.assertEqual(picked, ['show002:2:1', 'show002:1:1'])

    def test_urgent_positive_renewal_precedes_type_fairness(self):
        self.add_movies(1)
        self.add_shows(1)
        self.app.store.record('series', 'show000:2:1', 'show000',
                              'available', 1, time.time() - 40000)
        row = self.app.next_check('movie')
        self.assertEqual((row['type'], row['id']), ('series', 'show000:2:1'))

    def test_expired_error_positives_share_normal_type_budget(self):
        self.add_movies(2)
        self.add_shows(1, episodes=1)
        now = time.time()
        for season in (1, 2):
            ident = 'show000:%d:1' % season
            self.app.store.record('series', ident, 'show000', 'available', 1, now - 50000)
            self.app.store.record('series', ident, 'show000', 'error', 0, now - 1000)
        stored = self.app.store.db.execute("SELECT status,expires,failures FROM checks WHERE type='series'").fetchall()
        self.assertTrue(all(r['status'] == 'available' and r['expires'] < now and r['failures'] > 0 for r in stored))
        picked = []
        for kind in ('movie', 'series', 'movie', 'series'):
            row = self.app.next_check(kind)
            picked.append(row['type'])
            self.finish(row)
        self.assertEqual(picked, ['movie', 'series', 'movie', 'series'])

    def test_confirmation_inside_visibility_margin_is_not_urgent(self):
        self.add_movies(1)
        self.add_shows(1)
        now = time.time()
        self.app.store.record('series', 'show000:1:1', 'show000',
                              'available', 1, now - self.app.store.positive + 30)
        # Publishing already excludes the final60seconds of a confirmation.
        self.assertIsNone(self.app.store.meta('series', 'show000', now))
        self.assertEqual(self.app.next_check('movie')['type'], 'movie')

    def test_inflight_parent_does_not_hide_other_work_of_preferred_type(self):
        self.add_movies(3)
        first = self.app.next_check('movie')
        second = self.app.next_check('movie')
        third = self.app.next_check('movie')
        self.assertEqual({first['id'], second['id'], third['id']},
                         {'movie000', 'movie001', 'movie002'})
        self.assertIsNone(self.app.next_check('movie'))

    def test_queued_checks_from_inactive_shelf_are_not_returned(self):
        self.add_movies(1)
        self.add_shows(1)
        self.app.prioritize('series', ident='show000')
        first = self.app.next_check('series')
        self.finish(first)
        self.assertTrue(self.app.check_queue['series'])
        self.app.store.db.execute("UPDATE categories SET active=0 WHERE type='series'")
        self.app.store.db.commit()
        fallback = self.app.next_check('series')
        self.assertEqual(fallback['type'], 'movie')
        self.assertFalse(self.app.check_queue['series'])

    def test_fallback_uses_other_kind_when_preferred_has_no_work(self):
        self.add_shows(1)
        row = self.app.next_check('movie')
        self.assertEqual(row['type'], 'series')


if __name__ == '__main__':
    unittest.main()
