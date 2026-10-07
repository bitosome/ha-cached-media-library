import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store


class StoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/index.db'
        self.store = Store(self.path, positive=3600, max_episodes=2)
        self.now = time.time()
        self.catalogues = [
            {'id': 'source.movies', 'name': 'Movies', 'type': 'movie'},
            {'id': 'source.series', 'name': 'Series', 'type': 'series'},
        ]
        self.store.add_categories(self.catalogues, 'scanner')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def category(self, kind):
        return self.store.db.execute('SELECT * FROM categories WHERE type=?', (kind,)).fetchone()

    def add(self, kind, *previews):
        return self.store.add_page(self.category(kind), list(previews), 0, 1000)

    def videos(self, ident='tt1', count=8):
        return [{'id': '%s:1:%d' % (ident, number), 'season': 1, 'episode': number,
                 'released': '2020-01-01T00:00:00Z'} for number in range(1, count + 1)]

    def test_all_episodes_persist_and_chronological_priority_survives_refresh(self):
        self.add('series', {'id': 'tt1', 'name': 'Show'})
        videos = self.videos()
        videos.append({'id': 'tt1:0:1', 'season': 0, 'episode': 1, 'released': '2020-01-01T00:00:00Z'})
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': videos[::-1]}, self.now)
        rows = self.store.db.execute('SELECT id FROM checks ORDER BY priority').fetchall()
        self.assertEqual([r[0] for r in rows], [v['id'] for v in videos])
        self.store.record('series', 'tt1:1:8', 'tt1', 'available', 1, self.now)
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': videos}, self.now + 10)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 9)
        self.assertEqual([v['id'] for v in self.store.meta('series', 'tt1', self.now + 11)['videos']], ['tt1:1:8'])

    def test_bad_metadata_preserves_prior_metadata_and_checks(self):
        self.add('series', {'id': 'tt1', 'name': 'Show'})
        original = {'id': 'tt1', 'name': 'Good metadata', 'videos': self.videos()}
        self.store.save_meta('series', 'tt1', original, self.now)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.now)
        for invalid in (None, {}, {'id': 'tt1', 'videos': []}, {'id': 'tt1', 'videos': 'bad'},
                        {'id': 'tt1', 'videos': [None]}, {'id': 'tt1', 'videos': [{'id': 'broken'}]}):
            self.store.save_meta('series', 'tt1', invalid, self.now + 20)
            row = self.store.db.execute("SELECT meta,meta_due FROM titles WHERE id='tt1'").fetchone()
            self.assertEqual(json.loads(row['meta']), original)
            self.assertEqual(row['meta_due'], self.now + 1820)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 8)

    def test_episode_cleanup_is_parent_scoped(self):
        self.add('series', {'id': 'tt1', 'name': 'Show'}, {'id': 'tmdb:1', 'name': 'Alias'})
        for ident in ('tt1', 'tmdb:1'):
            self.store.save_meta('series', ident, {'id': 'tt1', 'videos': self.videos(count=2)}, self.now)
        self.store.record('series', 'tt1:1:2', 'tmdb:1', 'available', 1, self.now)
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': self.videos(count=1)}, self.now + 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM checks WHERE parent='tt1'").fetchone()[0], 1)
        self.assertTrue(self.store.visible('series', 'tmdb:1', self.now + 2))

    def test_russian_and_original_aliases_survive_catalog_and_metadata_refresh(self):
        preview = {'id': 'tt2', 'name': 'The Bear'}
        self.add('movie', preview)
        self.store.record('movie', 'tt2', 'tt2', 'available', 1, self.now)
        self.store.save_meta('movie', 'tt2', {'id': 'tt2', 'name': 'Медведь', 'original_title': 'Björn',
                                           'alternativeTitles': [{'title': 'Oso'}]}, self.now)
        self.store.save_meta('movie', 'tt2', {'id': 'tt2', 'name': 'The Bear'}, self.now + 1)
        self.add('movie', preview)
        for search in ('МЕДВЕДЬ', 'björn', 'oso', 'the bear'):
            self.assertEqual(len(self.store.catalog('movie', 'cached-search', search=search, now=self.now + 2)), 1)

    def test_restored_category_recrawls_immediately_and_rejects_old_response(self):
        self.add('movie', {'id': 'tt1', 'name': 'Old'})
        old = self.category('movie')
        self.assertEqual(old['done'], 1)
        self.store.add_categories(self.catalogues[1:], 'scanner')
        self.assertFalse(self.store.add_page(old, [{'id': 'ttghost', 'name': 'Ghost'}], 0, 1000))
        self.store.add_categories(self.catalogues, 'scanner')
        restored = self.category('movie')
        self.assertEqual((restored['offset'], restored['done'], restored['refresh']), (0, 0, 0))
        self.assertFalse(self.store.add_page(old, [{'id': 'ttghost', 'name': 'Ghost'}], 0, 1000))
        self.assertTrue(self.add('movie', {'id': 'ttnew', 'name': 'New'}))
        self.assertIsNone(self.store.db.execute("SELECT id FROM titles WHERE id='ttghost'").fetchone())

    def test_prior_crawl_generation_cannot_override_a_newer_page(self):
        snapshot = self.category('movie')
        self.assertTrue(self.store.add_page(snapshot, [{'id': 'tt1', 'name': 'Current'}], 0, 1000))
        self.assertFalse(self.store.add_page(snapshot, [{'id': 'tt2', 'name': 'Stale'}], 20, 1000))
        self.assertIsNone(self.store.db.execute("SELECT id FROM titles WHERE id='tt2'").fetchone())

    def test_inactive_membership_never_serves_or_accepts_new_verdicts(self):
        self.add('movie', {'id': 'tt1', 'name': 'Hidden', 'genres': ['Drama']})
        self.store.record('movie', 'tt1', 'tt1', 'available', 1, self.now)
        self.store.db.execute("UPDATE categories SET active=0 WHERE type='movie'")
        self.store.db.commit()
        self.assertFalse(self.store.visible('movie', 'tt1', self.now + 1))
        self.assertIsNone(self.store.meta('movie', 'tt1', self.now + 1))
        self.assertEqual(self.store.catalog('movie', self.category('movie')['id'], now=self.now + 1), [])
        self.assertEqual(self.store.catalog('movie', 'cached-search', search='Hidden', now=self.now + 1), [])
        self.assertEqual(self.store.genres('movie', self.now + 1), [])
        self.store.record('movie', 'tt1', 'tt1', 'unavailable', 0, self.now + 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM checks').fetchone()[0], 'available')

    def test_migration_repairs_metadata_once_without_resetting_confirmation(self):
        self.add('series', {'id': 'tt1', 'name': 'Show'})
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': self.videos()}, self.now)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.now)
        self.store.db.execute("DELETE FROM settings WHERE key='store_schema_version'")
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.path)
        row = self.store.db.execute('SELECT meta_due,meta_checked,meta_claimed,scan_touched FROM titles').fetchone()
        self.assertEqual(tuple(row), (0, 0, 0, 0))
        self.assertTrue(self.store.visible('series', 'tt1', self.now + 1))
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': self.videos()}, self.now + 2)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute('SELECT meta_checked FROM titles').fetchone()[0], self.now + 2)

    def test_alias_dedup_preserves_a_resolvable_identifier_and_genres(self):
        self.add('movie', {'id': 'tt1', 'name': 'Title', 'genres': ['Comedy']},
                 {'id': 'tmdb:1', 'name': 'Title'}, {'id': 'tt2', 'name': 'Same name, different movie'})
        for ident in ('tt1', 'tmdb:1', 'tt2'):
            self.store.record('movie', ident, ident, 'available', 1, self.now)
            self.store.save_meta('movie', ident, {'id': 'tt1' if ident != 'tt2' else 'tt2',
                                                'genres': ['Family']}, self.now)
        items = self.store.catalog('movie', self.category('movie')['id'], now=self.now + 1)
        self.assertEqual([item['id'] for item in items], ['tt1', 'tt2'])
        self.assertEqual(items[0]['genres'], ['Comedy', 'Family'])
        self.assertIsNotNone(self.store.meta('movie', items[0]['id'], self.now + 1))
        self.assertEqual(self.store.genres('movie', self.now + 1), ['Comedy', 'Family'])


if __name__ == '__main__':
    unittest.main()
