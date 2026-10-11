import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store


class SearchPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + '/db', positive=3600)
        self.now = time.time()
        self.shelves = [{'id': 'ordinary.movies', 'type': 'movie', 'name': 'Movies'}]
        self.store.add_categories(self.shelves, 'scanner')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def add(self, previews, **kwargs):
        return self.store.add_search_results('movie', previews, self.now, **kwargs)

    def ordinary(self, *previews):
        cat = self.store.db.execute('SELECT * FROM categories WHERE search_only=0').fetchone()
        self.store.add_page(cat, list(previews), 0, 1000)
        return cat['id']

    def confirm(self, ident, now=None):
        self.store.record('movie', ident, ident, 'available', 1, self.now if now is None else now)

    def proof(self, ident):
        row = self.store.db.execute('SELECT * FROM checks WHERE id=?', (ident,)).fetchone()
        return tuple(row) if row else None

    def test_search_candidates_are_hidden_until_confirmed_and_never_a_shelf(self):
        self.add([{'id': 'tt1', 'name': 'Puss in Boots', 'streams': [{'url': 'https://invalid/video'}],
                   'videos': [{'id': 'leaked'}], 'behaviorHints': {'defaultVideoId': 'wrong'}}])
        self.assertEqual(self.store.search_matches('movie', 'puss in the boots', now=self.now), ['tt1'])
        self.assertEqual(self.store.catalog('movie', 'cached-search', search='puss in the boots', now=self.now), [])
        self.assertIsNone(self.store.meta('movie', 'tt1', self.now))
        self.confirm('tt1')
        result = self.store.catalog('movie', 'cached-search', search='puss in the boots', now=self.now + 1)
        self.assertEqual([p['id'] for p in result], ['tt1'])
        for field in ('streams', 'videos', 'behaviorHints'):
            self.assertNotIn(field, result[0])
        self.assertEqual(self.store.catalog('movie', 'internal-search-movie', now=self.now + 1), [])
        self.assertFalse(any(c['search_only'] for c in self.store.exposed_categories()))
        self.assertFalse(any(c['name'] == 'Search candidates' for c in self.store.status()['categories']))
        self.assertEqual(self.store.status()['search_candidates'], {'movie': 1})
        self.assertEqual(self.store.catalog('movie', 'cached-search', search='puss', now=self.now + 3601), [])

    def test_source_reconciliation_and_crawl_changes_preserve_search_pool(self):
        self.add([{'id': 'tt1', 'name': 'Found'}])
        self.confirm('tt1')
        proof = self.proof('tt1')
        self.store.add_categories([], 'scanner')
        self.store.configure_crawl(400, 1, 'changed')
        self.assertEqual(self.proof('tt1'), proof)
        hidden = self.store.db.execute('SELECT * FROM categories WHERE search_only=1').fetchone()
        self.assertEqual((hidden['active'], hidden['done'], hidden['offset']), (1, 1, 0))
        self.assertTrue(self.store.visible('movie', 'tt1', self.now + 1))

    def test_alias_refresh_keeps_display_metadata_and_original_confirmation(self):
        self.ordinary({'id': 'tt1', 'name': 'Puss in Boots', 'poster': 'https://images.invalid/english.jpg'})
        self.store.save_meta('movie', 'tt1', {'id': 'tt1', 'name': 'Original Metadata'}, self.now)
        self.confirm('tt1')
        proof = self.proof('tt1')
        old = self.store.db.execute('SELECT preview,meta FROM titles WHERE id=?', ('tt1',)).fetchone()
        self.add([{'id': 'tt1', 'name': 'Кот в сапогах', 'original_title': 'Puss in Boots'}])
        fresh = self.store.db.execute('SELECT preview,meta FROM titles WHERE id=?', ('tt1',)).fetchone()
        self.assertEqual(tuple(old), tuple(fresh))
        self.assertEqual(self.proof('tt1'), proof)
        self.assertEqual(self.store.catalog('movie', 'cached-search', search='кот в сапогах', now=self.now)[0]['name'], 'Puss in Boots')

    def test_pool_expiry_and_cap_remove_only_orphans(self):
        self.ordinary({'id': 'tt1', 'name': 'Shelf title'})
        self.confirm('tt1')
        proof = self.proof('tt1')
        self.add([{'id': 'tt1', 'name': 'Shelf title'}, {'id': 'tt2', 'name': 'Search title'}], ttl=60, cap=2)
        self.confirm('tt2')
        self.assertEqual(self.store.prune_search_results(self.now + 61), 2)
        self.assertEqual(self.proof('tt1'), proof)
        self.assertIsNone(self.proof('tt2'))
        self.assertTrue(self.store.visible('movie', 'tt1', self.now + 61))
        self.assertFalse(self.store.visible('movie', 'tt2', self.now + 61))
        self.store.add_search_results('movie', [{'id': 'tt3', 'name': 'Old'}], self.now, cap=1)
        self.store.add_search_results('movie', [{'id': 'tt4', 'name': 'New'}], self.now + 1, cap=1)
        self.assertIsNone(self.proof('tt3'))
        self.assertIsNotNone(self.proof('tt4'))
        self.assertEqual(self.proof('tt1'), proof)

    def test_read_expiration_does_not_wait_for_periodic_cleanup(self):
        self.add([{'id': 'tt1', 'name': 'Ephemeral'}], ttl=60)
        self.confirm('tt1')
        self.assertFalse(self.store.visible('movie', 'tt1', self.now + 61))
        self.assertEqual(self.store.search_matches('movie', 'ephemeral', now=self.now + 61), [])

    def test_reads_do_not_commit_a_callers_transaction(self):
        self.add([{'id': 'tt1', 'name': 'Temporary'}], ttl=60)
        self.confirm('tt1')
        self.store.db.execute("UPDATE checks SET count=9 WHERE id='tt1'")
        self.assertTrue(self.store.visible('movie', 'tt1', self.now))
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertEqual(self.store.db.execute("SELECT count FROM checks WHERE id='tt1'").fetchone()[0], 1)
        self.store.db.execute("UPDATE checks SET count=9 WHERE id='tt1'")
        self.assertFalse(self.store.visible('movie', 'tt1', self.now + 61))
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertIsNotNone(self.proof('tt1'))

    def test_series_waits_for_metadata_and_confirmed_aired_episode(self):
        self.store.add_search_results('series', [{'id': 'tt1', 'name': 'Series'}], self.now)
        self.assertEqual(self.store.search_matches('series', 'series', now=self.now), ['tt1'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM checks WHERE type='series'").fetchone()[0], 0)
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': [
            {'id': 'tt1:1:1', 'season': 1, 'episode': 1, 'released': '2020-01-01'},
            {'id': 'tt1:1:2', 'season': 1, 'episode': 2, 'released': '2020-01-01'}]}, self.now)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.now)
        self.assertEqual([v['id'] for v in self.store.meta('series', 'tt1', self.now)['videos']], ['tt1:1:1'])

    def test_relevance_dedup_and_pagination_use_only_fresh_confirmations(self):
        previews = [{'id': 'tt%04d' % i, 'name': 'Puss in Boots %d' % i} for i in range(102)]
        previews += [{'id': 'tt9000', 'name': 'Puss in Boots'}, {'id': 'tmdb:9000', 'name': 'Puss in Boots'}]
        self.add(previews)
        for p in previews:
            self.confirm(p['id'])
        self.store.save_meta('movie', 'tmdb:9000', {'id': 'tt9000', 'imdb_id': 'tt9000'}, self.now)
        first = self.store.catalog('movie', 'cached-search', search='puss in boots', now=self.now)
        second = self.store.catalog('movie', 'cached-search', search='puss in boots', skip=100, now=self.now)
        self.assertEqual(first[0]['name'], 'Puss in Boots')
        self.assertEqual((len(first), len(second)), (100, 3))
        self.assertFalse(set(p['id'] for p in first) & set(p['id'] for p in second))
        self.store.record('movie', 'tt0000', 'tt0000', 'unavailable', 0, self.now + 1)
        results = self.store.catalog('movie', 'cached-search', search='puss in boots 0', now=self.now + 2)
        self.assertFalse(any(p['id'] == 'tt0000' for p in results))

    def test_invalid_candidates_cannot_write_stream_or_episode_identities(self):
        for preview in ({'id': 'https://invalid/x', 'name': 'URL'}, {'id': 'tt1:1:1', 'name': 'Episode'},
                        {'id': 'tmdb:0', 'name': 'Bad'}, {'id': 'tt1', 'name': ''},
                        {'id': 'tt1', 'name': 'Wrong type', 'type': 'series'}):
            with self.subTest(preview=preview), self.assertRaises(ValueError):
                self.add([preview])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM titles').fetchone()[0], 0)
