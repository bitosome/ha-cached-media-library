"""Explicit source revision changes re-crawl selections without losing proofs."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App, Store


class CatalogRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + '/db')
        self.catalogues = [
            {'id': 'movies', 'type': 'movie', 'name': 'Movies', 'extra': [{'name': 'skip'}]},
            {'id': 'curated', 'type': 'movie', 'name': 'Curated'},
        ]
        self.store.add_categories(self.catalogues, 'scanner')
        self.store.configure_crawl(400, 6)
        self.store.add_page(self.category('movies'), self.previews(0, 400), 0, 400)
        self.store.add_page(self.category('curated'), self.previews(400, 412), 0, 400)
        self.store.record('movie', 'tt0000010', 'tt0000010', 'available', 2, time.time())

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def category(self, upstream):
        return self.store.db.execute('SELECT * FROM categories WHERE upstream=?', (upstream,)).fetchone()

    def previews(self, start, stop):
        return [{'id': 'tt%07d' % i, 'name': 'Movie %d' % i} for i in range(start, stop)]

    def evidence(self):
        return tuple(self.store.db.execute("SELECT * FROM checks WHERE id='tt0000010'").fetchone())

    def test_changed_revision_requeues_paginated_and_curated_without_resetting_evidence(self):
        old = {name: self.category(name) for name in ('movies', 'curated')}
        proof = self.evidence()
        self.assertEqual(self.store.configure_crawl(400, 6, 'selection-v1'), 2)
        for name in old:
            row = self.category(name)
            self.assertEqual((row['offset'], row['done'], row['refresh']), (0, 0, 0))
            self.assertGreater(row['generation'], old[name]['generation'])
            self.assertFalse(self.store.add_page(old[name], self.previews(900, 920), 0, 400))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 412)
        self.assertEqual(self.evidence(), proof)
        self.assertTrue(self.store.visible('movie', 'tt0000010', time.time()))

    def test_same_revision_does_not_restart_partial_crawl_or_completed_curated_list(self):
        self.store.configure_crawl(400, 6, 'selection-v1')
        self.store.add_page(self.category('movies'), self.previews(0, 20), 0, 400)
        self.store.add_page(self.category('curated'), self.previews(400, 412), 0, 400)
        before = [tuple(self.category(name)) for name in ('movies', 'curated')]
        self.assertEqual(self.store.configure_crawl(400, 6, 'selection-v1'), 0)
        self.assertEqual([tuple(self.category(name)) for name in ('movies', 'curated')], before)
        self.assertEqual(self.store.setting('crawl_settings')['revision'], 'selection-v1')
        self.assertEqual(self.store.configure_crawl(400, 6, 'selection-v2'), 2)

    def test_missing_saved_revision_is_equivalent_to_empty_default(self):
        self.store.setting('crawl_settings', {'max_candidates': 400, 'refresh_hours': 6})
        before = [tuple(self.category(name)) for name in ('movies', 'curated')]
        proof = self.evidence()
        self.assertEqual(self.store.configure_crawl(400, 6), 0)
        self.assertEqual([tuple(self.category(name)) for name in ('movies', 'curated')], before)
        self.assertEqual(self.evidence(), proof)
        self.assertEqual(self.store.configure_crawl(400, 6, '2026-10-08-selection-v1'), 2)

    def test_prior_selection_stays_until_new_nonempty_crawl_completes(self):
        original = self.category('movies')
        proof = self.evidence()
        self.store.configure_crawl(400, 6, 'selection-v1')
        selected = self.previews(0, 200) + self.previews(500, 700)
        self.store.add_page(self.category('movies'), selected[:20], 0, 400)
        self.assertIsNotNone(self.store.db.execute('SELECT 1 FROM membership WHERE category=? AND id=?',
                                                   (original['id'], 'tt0000350')).fetchone())
        for offset in range(20, 400, 20):
            self.store.add_page(self.category('movies'), selected[offset:offset + 20], offset, 400)
        ids = {r[0] for r in self.store.db.execute('SELECT id FROM membership WHERE category=?', (original['id'],))}
        self.assertEqual(ids, {p['id'] for p in selected})
        self.assertEqual(self.evidence(), proof)

    def test_revision_does_not_reactivate_retired_shelves(self):
        self.store.add_categories(self.catalogues[:1], 'scanner')
        self.assertEqual(self.store.configure_crawl(400, 6, 'selection-v1'), 1)
        self.assertEqual(self.category('curated')['active'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership WHERE category=?',
                                               (self.category('curated')['id'],)).fetchone()[0], 0)

    def test_app_synchronization_passes_revision_and_keeps_policy_proof(self):
        options = {'aiostreams_url': 'http://aio.invalid', 'active_uuid': 'active', 'active_password': 'p',
                   'stremio_uuid': 'active', 'stremio_encrypted_password': 'e',
                   'catalog_uuid': 'source', 'catalog_encrypted_password': 'e',
                   'max_candidates_per_category': 400, 'catalog_refresh_hours': 1}
        app = App(options, self.temp.name + '/appdb')
        config = {'excludeUncached': True, 'requiredStreamExpressions': [
            {'enabled': True, 'expression': "cached(service(streams, 'torbox'))"}]}
        app.request = lambda *a, **k: {'data': {'userData': config}}
        app.upstream_catalogs = lambda: self.catalogues
        try:
            app.synchronize()
            cat = app.store.db.execute("SELECT * FROM categories WHERE upstream='movies'").fetchone()
            app.store.add_page(cat, self.previews(0, 400), 0, 400)
            app.store.record('movie', 'tt0000010', 'tt0000010', 'available', 1, time.time())
            proof = tuple(app.store.db.execute("SELECT * FROM checks WHERE id='tt0000010'").fetchone())
            app.o['catalog_revision'] = 'selection-v1'
            app.synchronize()
            self.assertEqual(app.store.setting('crawl_settings')['revision'], 'selection-v1')
            self.assertEqual(app.store.db.execute("SELECT done FROM categories WHERE upstream='movies'").fetchone()[0], 0)
            self.assertEqual(tuple(app.store.db.execute("SELECT * FROM checks WHERE id='tt0000010'").fetchone()), proof)
        finally:
            app.store.db.close()


if __name__ == '__main__':
    unittest.main()
