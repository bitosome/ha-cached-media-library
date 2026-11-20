"""Newest-first shelf display order (0.12.0)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store, recency


class ShelfOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + '/db', positive=3600, negative=7200)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.db.close())
        self.now = time.time()

    def shelf(self, kind, previews):
        self.store.add_categories([{'id': 'shelf', 'type': kind, 'name': 'Shelf'}], 'scanner')
        cat = self.store.db.execute('SELECT * FROM categories').fetchone()
        self.store.add_page(cat, previews, 0, 100)
        return cat['id']

    def names(self, kind, cid, **extra):
        return [m['name'] for m in self.store.catalog(kind, cid, now=self.now + 1, **extra)]

    def test_movies_are_newest_first_and_ties_keep_the_source_rank(self):
        cid = self.shelf('movie', [{'id': 'tt1', 'name': 'Old', 'releaseInfo': '1994'},
                                   {'id': 'tt2', 'name': 'Undated'},
                                   {'id': 'tt3', 'name': 'New', 'releaseInfo': '2026'},
                                   {'id': 'tt4', 'name': 'Also new', 'releaseInfo': '2026'}])
        for ident in ('tt1', 'tt2', 'tt3', 'tt4'):
            self.store.record('movie', ident, ident, 'available', 1, self.now)
        self.store.save_meta('movie', 'tt4', {'id': 'tt4', 'released': '2026-10-01T00:00:00.000Z'}, self.now)
        self.assertEqual(self.names('movie', cid), ['Old', 'Undated', 'New', 'Also new'])
        self.assertEqual(self.names('movie', cid, order='newest'), ['Also new', 'New', 'Old', 'Undated'])
        self.assertEqual(self.names('movie', cid, order='newest', skip=1), ['New', 'Old', 'Undated'])

    def test_series_sort_by_latest_aired_regular_episode(self):
        meta = lambda ident, dates: {'id': ident, 'videos': [
            {'id': '%s:%d:1' % (ident, s), 'season': s, 'episode': 1, 'released': d} for s, d in dates]}
        old_show = meta('tt1', [(1, '2008-01-01T00:00:00Z'), (5, '2026-11-01T00:00:00Z'), (0, '2026-11-15T00:00:00Z')])
        newer_show = meta('tt2', [(1, '2025-03-01T00:00:00Z'), (2, '2099-01-01T00:00:00Z')])
        self.assertEqual(recency('series', {}, old_show, self.now), '2026-11-01')
        self.assertEqual(recency('series', {}, newer_show, self.now), '2025-03-01')  # Future episodes ignored.
        self.assertEqual(recency('series', {'releaseInfo': '2008–2013'}, {}, self.now), '2013-00-00')
        self.assertEqual(recency('movie', {}, {}, self.now), '')

    def test_search_keeps_relevance_order(self):
        cid = self.shelf('movie', [{'id': 'tt1', 'name': 'Alien', 'releaseInfo': '1979'},
                                   {'id': 'tt2', 'name': 'Alien Romulus', 'releaseInfo': '2024'}])
        for ident in ('tt1', 'tt2'):
            self.store.record('movie', ident, ident, 'available', 1, self.now)
        self.assertEqual(self.names('movie', cid, order='newest'), ['Alien Romulus', 'Alien'])
        found = [m['name'] for m in self.store.catalog('movie', 'cached-search', search='Alien',
                                                       now=self.now + 1, order='newest')]
        self.assertEqual(found[0], 'Alien')


if __name__ == '__main__':
    unittest.main()
