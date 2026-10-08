"""Native discovery must replace the provider without resetting the library."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App, STRICT_FILTER


class NativeDiscoveryAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = {'id': 'oldprovider.catalog-id', 'type': 'movie', 'name': 'Best Movies',
                        'filters': {'listType': 'discover', 'sortBy': 'vote_average.desc',
                                    'ratingMin': 7, 'voteCountMin': 500}}
        self.options = {'aiostreams_url': 'http://aio.invalid', 'active_uuid': 'active',
                        'active_password': 'p', 'stremio_uuid': 'active', 'stremio_encrypted_password': 'e',
                        'catalog_uuid': 'source', 'catalog_encrypted_password': 'e',
                        'tmdb_api_key': 'a' * 32, 'tmdb_catalogs': json.dumps([self.catalog]),
                        'max_candidates_per_category': 1000}
        self.app = App(self.options, self.temp.name + '/db')
        self.config = {'excludeUncached': True, 'requiredStreamExpressions': [
            {'enabled': True, 'expression': STRICT_FILTER}]}
        self.remote_catalogs = [{'id': self.catalog['id'], 'type': 'movie', 'name': 'Old Movies'},
                                {'id': 'family', 'type': 'movie', 'name': 'Family'}]
        def request(path, **kwargs):
            if kwargs.get('config'):
                return {'data': {'userData': self.config}}
            if path == '/manifest.json':
                return {'catalogs': self.remote_catalogs}
            self.fail('Native catalogue must not request AIOStreams: ' + path)
        self.app.request = request

    def tearDown(self):
        self.app.stop.set()
        self.app.store.db.close()
        self.temp.cleanup()

    def test_retiring_hosted_provider_preserves_shelf_and_proofs(self):
        self.app.synchronize()
        cat = self.app.store.db.execute('SELECT * FROM categories WHERE upstream=?', (self.catalog['id'],)).fetchone()
        self.app.store.add_page(cat, [{'id': 'tt1234567', 'name': 'Existing'}], 0, 1)
        self.app.store.record('movie', 'tt1234567', 'tt1234567', 'available', 3, time.time())
        before = tuple(self.app.store.db.execute('SELECT * FROM checks').fetchone())
        self.remote_catalogs = self.remote_catalogs[1:]
        self.app.synchronize()
        current = self.app.store.db.execute('SELECT * FROM categories WHERE upstream=?', (self.catalog['id'],)).fetchone()
        self.assertEqual((cat['id'], 1), (current['id'], current['active']))
        self.assertEqual(before, tuple(self.app.store.db.execute('SELECT * FROM checks').fetchone()))
        self.assertEqual(len(self.app.upstream_catalogs()), 2)

    def test_empty_source_manifest_does_not_retire_curated_shelves(self):
        self.app.synchronize()
        before = [tuple(row) for row in self.app.store.db.execute('SELECT * FROM categories')]
        self.remote_catalogs = []
        with self.assertRaisesRegex(ValueError, 'source catalogue manifest'):
            self.app.synchronize()
        self.assertEqual(before, [tuple(row) for row in self.app.store.db.execute('SELECT * FROM categories')])

    def test_identity_mapping_is_persistent_and_does_not_extend_stream_proof(self):
        with patch.object(self.app.tmdb, 'external_id', return_value='tt1234567') as fetch:
            self.assertEqual(self.app.resolve_tmdb_identity('movie', 42), 'tt1234567')
            self.assertEqual(self.app.resolve_tmdb_identity('movie', 42), 'tt1234567')
            fetch.assert_called_once_with('movie', 42)
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 0)
        with patch.object(self.app.tmdb, 'external_id', return_value=None) as fetch:
            self.assertIsNone(self.app.resolve_tmdb_identity('series', 42))
            self.assertIsNone(self.app.resolve_tmdb_identity('series', 42))
            fetch.assert_called_once()

    def test_failed_identity_lookup_does_not_cache_absence(self):
        with patch.object(self.app.tmdb, 'external_id', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.app.resolve_tmdb_identity('movie', 42)
        self.assertIsNone(self.app.store.get_tmdb_identity('movie', 42, time.time()))

    def test_direct_crawl_uses_native_completion(self):
        self.app.synchronize()
        self.app.store.db.execute("UPDATE categories SET done=1,refresh=? WHERE upstream='family'", (time.time() + 1000,))
        self.app.store.db.commit()
        def page(*args):
            self.app.stop.set()
            return {'metas': [{'id': 'tt1234567', 'name': 'Native'}], 'complete': True, 'next_offset': 1}
        with patch.object(self.app.tmdb, 'catalog', side_effect=page) as fetch:
            self.app.crawl_loop()
        fetch.assert_called_once_with('movie', self.catalog['id'], 0)
        cat = self.app.store.db.execute('SELECT * FROM categories WHERE upstream=?', (self.catalog['id'],)).fetchone()
        self.assertEqual((cat['done'], cat['offset']), (1, 1))
        self.assertFalse(self.app.store.visible('movie', 'tt1234567', time.time()))

    def test_tmdb_failure_preserves_membership_and_confirmation(self):
        self.app.synchronize()
        cat = self.app.store.db.execute('SELECT * FROM categories WHERE upstream=?', (self.catalog['id'],)).fetchone()
        self.app.store.add_page(cat, [{'id': 'tt1234567', 'name': 'Native'}], 0, 1000)
        self.app.store.record('movie', 'tt1234567', 'tt1234567', 'available', 3, time.time())
        proof = tuple(self.app.store.db.execute('SELECT * FROM checks').fetchone())
        self.app.store.db.execute("UPDATE categories SET done=1,refresh=? WHERE upstream='family'", (time.time() + 1000,))
        self.app.store.db.commit()
        def fail(*args):
            self.app.stop.set()
            raise RuntimeError('transport failed')
        with patch.object(self.app.tmdb, 'catalog', side_effect=fail), patch('builtins.print'):
            self.app.crawl_loop()
        self.assertEqual(tuple(self.app.store.db.execute('SELECT * FROM checks').fetchone()), proof)
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM membership').fetchone()[0], 1)

    def test_local_filter_change_requeues_without_policy_reset(self):
        self.app.synchronize()
        before = self.app.store.setting('policy')
        rev = self.app.crawl_revision()
        self.app.o['tmdb_catalogs'] = json.dumps([dict(self.catalog, filters={'voteCountMin': 1000})])
        self.assertNotEqual(rev, self.app.crawl_revision())
        self.assertEqual(before, self.app.store.setting('policy'))


if __name__ == '__main__':
    unittest.main()
