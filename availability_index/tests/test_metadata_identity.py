"""Season navigation must use the same title identity as catalogue/search."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store


class MetadataIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + '/db', positive=3600)
        self.now = time.time()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.db.close)
        self.store.add_categories([{'id': 'series', 'type': 'series', 'name': 'Series'},
                                   {'id': 'movies', 'type': 'movie', 'name': 'Movies'}], 'scanner')

    def seed(self, parent, upstream, kind='series', extra=None):
        cat = self.store.db.execute('SELECT * FROM categories WHERE type=?', (kind,)).fetchone()
        self.store.add_page(cat, [{'id': parent, 'name': 'Bluey', 'imdb_id': 'tt7678620'}], 0, 100)
        meta = {'id': upstream, 'type': kind, 'name': 'Bluey', **(extra or {})}
        if kind == 'series':
            meta['videos'] = [
                {'id': 'tt7678620:1:1', 'season': 1, 'episode': 1, 'released': '2018-10-01T00:00:00Z'},
                {'id': 'tt7678620:1:2', 'season': 1, 'episode': 2, 'released': '2018-10-02T00:00:00Z'},
                {'id': 'tt7678620:2:1', 'season': 2, 'episode': 1, 'released': '2020-01-01T00:00:00Z'},
                {'id': 'tt7678620:9:1', 'season': 9, 'episode': 1, 'released': '2999-01-01T00:00:00Z'},
            ]
        self.store.save_meta(kind, parent, meta, self.now)
        for ident in (['tt7678620:1:1', 'tt7678620:2:1'] if kind == 'series' else [parent]):
            self.store.record(kind, ident, parent, 'available', 4, self.now)
        return meta

    def snapshot(self):
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)]
                for table in ['titles', 'checks', 'membership']}

    def test_imdb_search_parent_stays_consistent_with_seasons_and_episodes(self):
        original = self.seed('tt7678620', 'tmdb:82728', extra={'imdb_id': 'tt7678620'})
        before = self.snapshot()
        hit = self.store.catalog('series', 'cached-search', search='bluey', now=self.now)[0]
        meta = self.store.meta('series', hit['id'], self.now)
        self.assertEqual(meta['id'], hit['id'])
        self.assertEqual(meta['tmdb_id'], '82728')
        self.assertEqual(meta['imdb_id'], 'tt7678620')
        self.assertEqual([v['id'] for v in meta['videos']], ['tt7678620:1:1', 'tt7678620:2:1'])
        self.assertEqual({v['season'] for v in meta['videos']}, {1, 2})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads(self.store.db.execute("SELECT meta FROM titles WHERE id='tt7678620'").fetchone()[0]), original)
        # The fix must not manufacture availability for an unindexed alias.
        self.assertIsNone(self.store.meta('series', 'tmdb:82728', self.now))
        self.assertIsNone(self.store.meta('series', hit['id'], self.now + 3601))

    def test_tmdb_search_parent_retains_upstream_imdb_alias_and_episode_ids(self):
        self.seed('tmdb:82728', 'tt7678620')
        before = self.snapshot()
        meta = self.store.meta('series', 'tmdb:82728', self.now)
        self.assertEqual(meta['id'], 'tmdb:82728')
        self.assertEqual(meta['imdb_id'], 'tt7678620')
        self.assertEqual([v['id'] for v in meta['videos']], ['tt7678620:1:1', 'tt7678620:2:1'])
        self.assertEqual(self.snapshot(), before)

    def test_explicit_provider_identity_fields_are_never_overwritten(self):
        extra = {'ids': {'tmdb': '82728', 'imdb': 'tt7678620'}, 'tmdb_id': '82728',
                 'imdb_id': 'tt7678620', 'behaviorHints': {'defaultVideoId': None}}
        self.seed('tt7678620', 'tmdb:82728', extra=extra)
        meta = self.store.meta('series', 'tt7678620', self.now)
        for field, value in extra.items():
            self.assertEqual(meta[field], value)
        self.assertEqual(meta['id'], 'tt7678620')

    def test_movie_parent_preserves_explicit_playback_target(self):
        self.seed('tt1234567', 'tmdb:42', kind='movie',
                  extra={'behaviorHints': {'defaultVideoId': 'tt1234567'}})
        before = self.snapshot()
        meta = self.store.meta('movie', 'tt1234567', self.now)
        self.assertEqual((meta['id'], meta['tmdb_id']), ('tt1234567', '42'))
        self.assertEqual(meta['behaviorHints']['defaultVideoId'], 'tt1234567')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
