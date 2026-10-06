import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App, Store, aired, episode_order, is_scanner_catalog, policy_fingerprint, stream_verdict

import time

NOW = time.time()
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name + '/db', positive=3600, negative=7200, max_episodes=2)
        self.s.add_categories([{'id': 'upstream.one', 'type': 'movie', 'name': 'Movies',
                                'extra': [{'name': 'skip'}]}], 'cachedlibrary')
        self.cat = self.s.db.execute('SELECT * FROM categories').fetchone()

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def add(self, count=1):
        self.s.add_page(self.cat, [{'id': 'tt%07d' % i, 'name': 'Movie %d' % i} for i in range(count)], 0, 1000)

    def test_filter_before_pagination_and_search(self):
        self.add(220)
        for i in range(220):
            self.s.record('movie', 'tt%07d' % i, 'available' if i % 2 else 'unavailable', 1, 1000)
        first = self.s.catalog('movie', self.cat['id'], now=1001)
        second = self.s.catalog('movie', self.cat['id'], skip=100, now=1001)
        self.assertEqual(len(first), 100)
        self.assertEqual(len(second), 10)
        self.assertFalse(set(x['id'] for x in first) & set(x['id'] for x in second))
        self.assertEqual(self.s.catalog('movie', 'cached-search', search='Movie 2', now=1001)[0]['name'], 'Movie 21')

    def test_failure_does_not_extend_positive_and_policy_invalidates(self):
        self.add()
        self.s.record('movie', 'tt0000000', 'available', 1, 1000)
        self.s.record('movie', 'tt0000000', 'error', 0, 4000)
        self.assertIsNotNone(self.s.meta('movie', 'tt0000000', 4001))
        self.assertIsNone(self.s.meta('movie', 'tt0000000', 4601))
        self.s.invalidate()
        self.assertIsNone(self.s.meta('movie', 'tt0000000', 1001))

    def test_uncached_and_unknown_never_visible(self):
        self.add(3)
        self.s.record('movie', 'tt0000000', 'error', 0, 1000)
        self.s.record('movie', 'tt0000001', 'unavailable', 0, 1000)
        self.assertEqual(self.s.catalog('movie', self.cat['id'], now=1001), [])

    def test_series_only_confirmed_aired_episodes(self):
        self.s.add_categories([{'id': 'upstream.shows', 'type': 'series', 'name': 'Shows'}], 'cachedlibrary')
        cat = self.s.db.execute("SELECT * FROM categories WHERE type='series'").fetchone()
        self.s.add_page(cat, [{'id': 'tt123', 'name': 'Ёжик'}], 0, 100)
        videos = [{'id': 'tt123:1:%d' % i, 'season': 1, 'episode': i, 'released': PAST} for i in range(1, 4)]
        videos.append({'id': 'tt123:1:4', 'season': 1, 'episode': 4, 'released': FUTURE})
        self.s.save_meta('series', 'tt123', {'id': 'tt123', 'name': 'Ёжик', 'type': 'series', 'videos': videos}, NOW)
        # Unaired episodes are never queued, and the per-series cap is applied.
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM checks').fetchone()[0], 2)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM checks WHERE id=?', ('tt123:1:4',)).fetchone()[0], 0)
        self.assertIsNone(self.s.meta('series', 'tt123', NOW + 1))
        self.s.record('series', 'tt123:1:2', 'available', 1, NOW)
        meta = self.s.meta('series', 'tt123', NOW + 1)
        self.assertEqual([v['id'] for v in meta['videos']], ['tt123:1:2'])
        self.assertEqual(len(self.s.catalog('series', 'cached-search', search='ежик', now=NOW + 1)), 1)

    def test_known_categories_survive_switchover(self):
        # The family profile hides upstream catalogues once switch-over happens.
        self.s.add_categories([{'id': 'cachedlibrary1a2b.cached-abc', 'type': 'movie', 'name': 'Mine'}], 'cachedlibrary')
        names = [c['name'] for c in self.s.db.execute('SELECT name FROM categories')]
        self.assertEqual(names, ['Movies'])
        self.add()
        self.assertEqual(self.s.catalog('movie', self.cat['id'], now=1001), [])

    def test_scanner_catalogs_are_skipped(self):
        self.s.add_categories([
            {'id': 'cachedlibrary1a2b.cached-abc', 'type': 'movie', 'name': 'Mine'},
            {'id': 'upstream.two', 'type': 'movie', 'name': 'Searchable', 'extra': [{'name': 'search', 'isRequired': True}]},
            {'id': 'upstream.three', 'type': 'movie', 'name': 'Added', 'extra': []},
        ], 'cachedlibrary')
        names = sorted(c['name'] for c in self.s.db.execute('SELECT name FROM categories'))
        self.assertEqual(names, ['Added', 'Movies'])

    def test_stream_notices_and_timeouts(self):
        self.assertEqual(stream_verdict({'streams': [{'name': 'Removal Reasons', 'description': 'Excluded Uncached (8)'}]}), ('unavailable', 0))
        self.assertEqual(stream_verdict({'streams': [{'name': 'Provider error', 'description': 'Timeout'}]}), ('error', 0))
        self.assertEqual(stream_verdict({'streams': [{'infoHash': 'abc'}, {'externalUrl': 'https://example.com'}]}), ('unavailable', 0))
        self.assertEqual(stream_verdict({'streams': [{'url': 'https://example.com/play'}]}), ('available', 1))
        self.assertFalse(aired({'id': 'x', 'season': 1, 'episode': 1}, 1000))

    def test_policy_fingerprint_tracks_filters_only(self):
        base = {'excludeUncached': True, 'requiredStreamExpressions': [], 'sortCriteria': {'global': []}}
        self.assertEqual(policy_fingerprint(base), policy_fingerprint(dict(base, sortCriteria={'global': [{'key': 'x'}]})))
        self.assertNotEqual(policy_fingerprint(base), policy_fingerprint(dict(base, excludeUncached=False)))

    def test_episode_order_prefers_newest_season_openers(self):
        videos = [{'season': 1, 'episode': 5}, {'season': 2, 'episode': 1}, {'season': 1, 'episode': 1}, {'season': 2, 'episode': 4}]
        self.assertEqual([(v['season'], v['episode']) for v in sorted(videos, key=episode_order)], [(2, 1), (1, 1), (2, 4), (1, 5)])

    def test_scanner_catalog_detection(self):
        self.assertTrue(is_scanner_catalog('cachedlibrary1a2b.cached-abc', 'cachedlibrary'))
        self.assertTrue(is_scanner_catalog('other.cached-search', 'cachedlibrary'))
        self.assertFalse(is_scanner_catalog('familylivee3b0.tmdb-1', 'cachedlibrary'))

    def test_empty_catalogues_are_not_published(self):
        # A catalogue that finished crawling with nothing leaves no empty shelf.
        self.s.add_page(self.cat, [], 0, 1000)
        self.assertEqual(self.s.exposed_categories(), [])
        self.add(1)
        self.assertTrue(self.s.exposed_categories())

    def test_shelves_are_retired_when_the_source_drops_them(self):
        # The catalogue source profile is authoritative, so removing a shelf there
        # must remove it here instead of leaving a stale, frozen row.
        self.add(2)
        self.assertEqual([c['name'] for c in self.s.exposed_categories()], ['Movies'])
        self.s.add_categories([], 'cachedlibrary')
        self.assertEqual(self.s.exposed_categories(), [])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM membership').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM titles').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM checks').fetchone()[0], 0)
        # Re-adding the shelf brings it back, freshly crawled.
        self.s.add_categories([{'id': 'upstream.one', 'type': 'movie', 'name': 'Movies',
                                'extra': [{'name': 'skip'}]}], 'cachedlibrary')
        self.assertEqual([c['active'] for c in self.s.db.execute('SELECT active FROM categories')], [1])


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, **overrides):
        options = {'aiostreams_url': 'http://aiostreams:3000', 'active_uuid': 'u', 'active_password': 'p',
                   'stremio_uuid': 'u', 'stremio_encrypted_password': 'e', 'endpoint_token': ''}
        options.update(overrides)
        return App(options, self.tmp.name + '/db-' + str(abs(hash(str(overrides)))))

    def test_upstream_catalogue_filtering(self):
        app = self.app()
        self.assertTrue(app.is_upstream({'id': 'familylivee3b0.tmdb-x', 'type': 'movie'}))
        self.assertFalse(app.is_upstream({'id': 'cachedlibrary1a2b.cached-ab', 'type': 'movie'}))
        self.assertFalse(app.is_upstream({'id': 'jfmetae3b0.tmdb.search', 'type': 'movie',
                                          'extra': [{'name': 'search', 'isRequired': True}]}))
        self.assertFalse(app.is_upstream({'id': 'x', 'type': 'anime'}))

    def test_missing_token_is_generated_and_persisted(self):
        app = self.app()
        self.assertGreaterEqual(len(app.o['endpoint_token']), 24)
        again = App({'aiostreams_url': 'http://aiostreams:3000', 'active_uuid': 'u', 'active_password': 'p',
                     'stremio_uuid': 'u', 'stremio_encrypted_password': 'e', 'endpoint_token': ''},
                    self.tmp.name + '/db-' + str(abs(hash('{}'))))
        self.assertEqual(app.o['endpoint_token'], again.o['endpoint_token'])

    def test_configuration_problems_are_reported_not_raised(self):
        app = self.app(aiostreams_url='', active_uuid='', active_password='',
                       stremio_uuid='', stremio_encrypted_password='', endpoint_token='short')
        problems = app.config_problems()
        self.assertEqual(len(problems), 4)

    def test_catalog_source_defaults_to_the_active_profile(self):
        app = self.app()
        self.assertEqual(app.catalog_source, app.source)
        other = self.app(catalog_uuid='c', catalog_encrypted_password='k')
        self.assertNotEqual(other.catalog_source, other.source)
        self.assertTrue(other.catalog_source.endswith('/stremio/c/k'))

    def test_metadata_is_claimed_once(self):
        app = self.app()
        app.store.add_categories([{'id': 'upstream.shows', 'type': 'series', 'name': 'Shows'}], 'cachedlibrary')
        cat = app.store.db.execute('SELECT * FROM categories').fetchone()
        app.store.add_page(cat, [{'id': 'tt1', 'name': 'Show'}], 0, 100)
        first = app.claim_meta()
        self.assertIsNotNone(first)
        self.assertEqual(first['id'], 'tt1')
        # A second worker must not pick the same title while it is being fetched.
        self.assertIsNone(app.claim_meta())

    def test_metadata_ordering_is_breadth_first_with_curated_shelves_first(self):
        app = self.app()
        app.store.add_categories([
            {'id': 'upstream.a', 'type': 'series', 'name': 'Ordinary', 'extra': [{'name': 'skip'}]},
            {'id': 'upstream.b', 'type': 'series', 'name': 'Curated', 'extra': [{'name': 'skip'}]},
        ], 'cachedlibrary')
        cats = app.store.db.execute('SELECT * FROM categories ORDER BY position').fetchall()
        app.store.add_page(cats[0], [{'id': 'ttshallow', 'name': 'A'}], 0, 1000)
        app.store.add_page(cats[0], [{'id': 'ttdeep', 'name': 'B'}], 400, 1000)
        app.store.add_page(cats[1], [{'id': 'ttcurated', 'name': 'C'}], 0, 1000)
        order = [row[0] for row in app.store.db.execute(
            'SELECT id FROM titles ORDER BY priority,id').fetchall()]
        self.assertEqual(order, ['ttcurated', 'ttshallow', 'ttdeep'])

    def test_most_popular_candidates_are_checked_first(self):
        app = self.app()
        app.store.add_categories([{'id': 'upstream.a', 'type': 'movie', 'name': 'Shelf',
                                   'extra': [{'name': 'skip'}]}], 'cachedlibrary')
        cat = app.store.db.execute('SELECT * FROM categories').fetchone()
        # Page 2 is crawled first so the alphabetical order would be the wrong answer.
        app.store.add_page(cat, [{'id': 'tt9', 'name': 'Unpopular'}], 20, 1000)
        app.store.add_page(cat, [{'id': 'tt1', 'name': 'Popular'}], 0, 1000)
        first = app.next_check('movie')
        self.assertEqual(first['id'], 'tt1')

    def test_schema_migration_adds_new_columns(self):
        store = Store(self.tmp.name + '/migrated')
        columns = {row[1] for row in store.db.execute('PRAGMA table_info(titles)')}
        self.assertIn('priority', columns)
        self.assertIn('meta_claimed', columns)
        store.db.close()


if __name__ == '__main__':
    unittest.main()
