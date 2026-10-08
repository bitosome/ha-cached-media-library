"""Catalogue depth/refresh changes must not discard availability evidence."""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App, Store, policy_fingerprint


class CatalogRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + '/db')
        self.store.add_categories([{'id': 'movies', 'type': 'movie', 'name': 'Movies',
                                   'extra': [{'name': 'skip'}]}], 'scanner')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def category(self):
        return self.store.db.execute('SELECT * FROM categories').fetchone()

    def previews(self, start, stop):
        return [{'id': 'tt%07d' % i, 'name': 'Movie %d' % i} for i in range(start, stop)]

    def complete(self, depth=400):
        self.store.add_page(self.category(), self.previews(0, depth), 0, depth)
        self.store.record('movie', 'tt0000010', 'tt0000010', 'available', 2, time.time())

    def evidence(self):
        return tuple(self.store.db.execute("SELECT * FROM checks WHERE id='tt0000010'").fetchone())

    def test_depth_increase_requeues_completed_shelf_without_resetting_positive(self):
        self.store.configure_crawl(400, 6)
        self.complete()
        before = self.evidence()
        old = self.category()
        self.assertEqual(old['done'], 1)
        self.assertEqual(self.store.configure_crawl(1000, 6), 1)
        fresh = self.category()
        self.assertEqual((fresh['offset'], fresh['done'], fresh['refresh']), (0, 0, 0))
        self.assertGreater(fresh['generation'], old['generation'])
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 400)
        self.assertFalse(self.store.add_page(old, self.previews(400, 420), 400, 400))
        for start, stop in [(0, 400), (400, 800), (800, 1000)]:
            self.assertTrue(self.store.add_page(self.category(), self.previews(start, stop), start, 1000))
        self.assertEqual(self.category()['done'], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 1000)
        self.assertEqual(self.evidence(), before)

    def test_legacy_upgrade_recrawls_once_and_same_settings_do_not_restart_progress(self):
        self.complete()
        before = self.evidence()
        self.assertIsNone(self.store.setting('crawl_settings'))
        self.assertEqual(self.store.configure_crawl(400, 6), 1)
        self.assertEqual(self.evidence(), before)
        self.store.add_page(self.category(), self.previews(0, 20), 0, 400)
        state = tuple(self.category())
        self.assertEqual(self.store.configure_crawl(400, 6), 0)
        self.assertEqual(tuple(self.category()), state)
        self.assertEqual(self.store.setting('crawl_settings'), {'max_candidates': 400, 'refresh_hours': 6, 'revision': ''})

    def test_completion_and_interval_changes_use_configured_refresh(self):
        self.store.configure_crawl(400, 1)
        with patch('server.time.time', return_value=1000):
            self.store.add_page(self.category(), self.previews(0, 400), 0, 400)
        original = self.category()
        self.assertEqual(original['refresh'], 4600)
        self.assertEqual(self.store.configure_crawl(400, 6), 0)
        # Do not postpone an already scheduled crawl/retry. The longer interval
        # applies when that crawl completes.
        self.assertEqual(self.category()['refresh'], 4600)
        self.assertEqual(self.category()['generation'], original['generation'])
        with patch('server.time.time', return_value=2000):
            self.store.add_page(self.category(), self.previews(0, 400), 0, 400)
        self.assertEqual(self.category()['refresh'], 23600)
        self.store.configure_crawl(400, 1)
        self.assertEqual(self.category()['refresh'], 5600)

    def test_lengthening_refresh_interval_does_not_postpone_provider_retry(self):
        self.store.configure_crawl(400, 6)
        self.complete()
        with patch('server.time.time', return_value=2000):
            self.store.add_page(self.category(), [], 0, 400)
        self.assertEqual(self.category()['refresh'], 2600)
        self.store.configure_crawl(400, 24)
        self.assertEqual(self.category()['refresh'], 2600)

    def test_partial_last_page_respects_arbitrary_depth(self):
        self.store.configure_crawl(25, 6)
        self.store.add_page(self.category(), self.previews(0, 20), 0, 25)
        self.assertEqual((self.category()['offset'], self.category()['done']), (20, 0))
        self.store.add_page(self.category(), self.previews(20, 40), 20, 25)
        self.assertEqual((self.category()['offset'], self.category()['done']), (25, 1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 25)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 25)
        self.assertIsNone(self.store.db.execute("SELECT id FROM titles WHERE id='tt0000025'").fetchone())

    def test_crawl_at_depth_boundary_completes_even_without_another_page(self):
        self.store.configure_crawl(25, 1)
        self.store.add_page(self.category(), self.previews(0, 25), 0, 25)
        self.store.db.execute('UPDATE categories SET done=0,refresh=0')
        self.store.db.commit()
        with patch('server.time.time', return_value=2000):
            self.assertTrue(self.store.add_page(self.category(), [], 25, 25))
        self.assertEqual((self.category()['offset'], self.category()['done'], self.category()['refresh']), (25, 1, 5600))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 25)

    def test_empty_first_page_preserves_prior_members_and_confirmations(self):
        self.store.configure_crawl(400, 6)
        self.complete()
        before = self.evidence()
        generation = self.category()['generation']
        with patch('server.time.time', return_value=2000):
            self.store.add_page(self.category(), [], 0, 400)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 400)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.category()['generation'], generation)
        self.assertEqual(self.category()['refresh'], 2600)

    def test_empty_tail_cannot_prune_unseen_old_members(self):
        self.store.configure_crawl(400, 6)
        self.complete()
        before = self.evidence()
        self.store.configure_crawl(1000, 6)
        self.store.add_page(self.category(), self.previews(0, 200), 0, 1000)
        with patch('server.time.time', return_value=2000):
            self.store.add_page(self.category(), [], 200, 1000)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 400)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.category()['refresh'], 2600)

    def test_empty_tail_can_finish_when_every_member_was_seen(self):
        self.store.configure_crawl(1000, 1)
        self.store.add_page(self.category(), self.previews(0, 20), 0, 1000)
        with patch('server.time.time', return_value=2000):
            self.store.add_page(self.category(), [], 20, 1000)
        self.assertEqual(self.category()['done'], 1)
        self.assertEqual(self.category()['refresh'], 5600)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 20)

    def test_malformed_preview_does_not_erase_existing_shelf(self):
        self.complete()
        state = tuple(self.category())
        before = self.evidence()
        for bad in ([None], [{'id': None}], [{'id': ''}]):
            with self.assertRaises(ValueError):
                self.store.add_page(self.category(), bad, 0, 400)
        self.assertEqual(tuple(self.category()), state)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 400)

    def test_depth_change_does_not_needlessly_restart_nonpaginated_shelves(self):
        self.store.add_categories([{'id': 'short', 'type': 'movie', 'name': 'Curated'}], 'scanner')
        self.store.configure_crawl(400, 6)
        cat = self.store.db.execute("SELECT * FROM categories WHERE active=1").fetchone()
        self.store.add_page(cat, self.previews(0, 20), 0, 400)
        before = tuple(self.store.db.execute("SELECT * FROM categories WHERE active=1").fetchone())
        self.assertEqual(self.store.configure_crawl(1000, 6), 0)
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM categories WHERE active=1").fetchone()), before)

    def test_synchronization_applies_depth_change_without_policy_invalidation(self):
        options = {'aiostreams_url': 'http://aio.invalid', 'active_uuid': 'active', 'active_password': 'p',
                   'stremio_uuid': 'active', 'stremio_encrypted_password': 'e',
                   'catalog_uuid': 'source', 'catalog_encrypted_password': 'e',
                   'max_candidates_per_category': 400, 'catalog_refresh_hours': 1}
        app = App(options, self.temp.name + '/appdb')
        config = {'excludeUncached': True, 'requiredStreamExpressions': [
            {'enabled': True, 'expression': "cached(service(streams, 'torbox'))"}]}
        app.request = lambda *a, **k: {'data': {'userData': config}}
        app.upstream_catalogs = lambda: [{'id': 'movies', 'type': 'movie', 'name': 'Movies', 'extra': [{'name': 'skip'}]}]
        try:
            app.synchronize()
            cat = app.store.db.execute('SELECT * FROM categories').fetchone()
            app.store.add_page(cat, self.previews(0, 400), 0, 400)
            app.store.record('movie', 'tt0000010', 'tt0000010', 'available', 1, time.time())
            before = tuple(app.store.db.execute("SELECT * FROM checks WHERE id='tt0000010'").fetchone())
            app.o['max_candidates_per_category'] = 1000
            app.synchronize()
            self.assertEqual(app.store.db.execute('SELECT done FROM categories').fetchone()[0], 0)
            self.assertEqual(tuple(app.store.db.execute("SELECT * FROM checks WHERE id='tt0000010'").fetchone()), before)
            state = tuple(app.store.db.execute('SELECT * FROM categories').fetchone())
            app.synchronize()
            self.assertEqual(tuple(app.store.db.execute('SELECT * FROM categories').fetchone()), state)
            self.assertEqual(app.store.setting('policy'), policy_fingerprint(config))
        finally:
            app.store.db.close()


if __name__ == '__main__':
    unittest.main()
