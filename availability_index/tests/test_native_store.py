"""Native discovery pagination and identity cache preserve availability evidence."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store


class NativeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/db'
        self.store = Store(self.path)
        self.store.add_categories([{'id': 'native', 'type': 'movie', 'name': 'Native',
                                    'extra': [{'name': 'skip'}]}], 'scanner')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def cat(self):
        return self.store.db.execute('SELECT * FROM categories').fetchone()

    def page(self, ids, offset=0, **kwargs):
        return self.store.add_page(self.cat(), [{'id': ident, 'name': ident} for ident in ids],
                                   offset, 100, **kwargs)

    def members(self):
        return [row[0] for row in self.store.db.execute('SELECT id FROM membership ORDER BY rank')]

    def proof(self):
        return [tuple(row) for row in self.store.db.execute('SELECT * FROM checks ORDER BY id')]

    def existing(self):
        self.page(['tt1', 'tt2', 'tt3'], complete=True, next_offset=3)
        self.store.record('movie', 'tt1', 'tt1', 'available', 4, 1000)
        self.store.record('movie', 'tt2', 'tt2', 'available', 2, 1000)

    def test_authoritative_short_last_page_prunes_without_changing_proofs(self):
        self.existing()
        proof = self.proof()
        self.page(['tt1'], next_offset=20)
        self.assertEqual(self.members(), ['tt1', 'tt2', 'tt3'])
        self.page(['tt2'], 20, next_offset=24, complete=True)
        self.assertEqual(self.members(), ['tt1', 'tt2'])
        self.assertEqual((self.cat()['offset'], self.cat()['done']), (24, 1))
        self.assertEqual(self.proof(), proof)

    def test_authoritative_empty_first_page_removes_members_but_preserves_evidence(self):
        self.existing()
        proof = self.proof()
        old_generation = self.cat()['generation']
        self.page([], complete=True, next_offset=0)
        self.assertEqual(self.members(), [])
        self.assertEqual(self.proof(), proof)
        self.assertFalse(self.store.visible('movie', 'tt1', 1001))
        self.assertGreater(self.cat()['generation'], old_generation)
        self.assertEqual((self.cat()['offset'], self.cat()['done']), (0, 1))

    def test_authoritative_empty_tail_prunes_unseen(self):
        self.existing()
        self.page(['tt1'], next_offset=20)
        self.page([], 20, complete=True, next_offset=20)
        self.assertEqual(self.members(), ['tt1'])
        self.assertEqual(self.cat()['done'], 1)

    def test_all_filtered_page_continues_without_early_completion(self):
        self.existing()
        proof = self.proof()
        self.page([], next_offset=20)
        self.assertEqual((self.cat()['offset'], self.cat()['done']), (20, 0))
        self.assertEqual(self.members(), ['tt1', 'tt2', 'tt3'])
        self.assertEqual(self.proof(), proof)
        self.page(['tt1'], 20, next_offset=40)
        self.page(['tt1'], 40, next_offset=60)
        self.assertEqual((self.cat()['offset'], self.cat()['done']), (60, 0))
        self.page([], 60, next_offset=60, complete=True)
        self.assertEqual(self.members(), ['tt1'])

    def test_ambiguous_empty_response_cannot_prune(self):
        self.existing()
        proof = self.proof()
        self.page(['tt1'], next_offset=20)
        with patch('server.time.time', return_value=3000):
            self.page([], 20)
        self.assertEqual(self.members(), ['tt1', 'tt2', 'tt3'])
        self.assertEqual(self.proof(), proof)
        self.assertEqual(self.cat()['refresh'], 3600)

    def test_invalid_or_stale_cursor_cannot_mutate_crawl(self):
        self.page(['tt1'], next_offset=20)
        before = tuple(self.cat()), self.proof(), self.members()
        for cursor in (19, 20, True, '40', -1):
            with self.assertRaises(ValueError):
                self.page(['tt2'], 20, next_offset=cursor)
        self.assertFalse(self.page(['tt2'], 40, next_offset=60))
        self.assertFalse(self.page([], 0, complete=True, next_offset=0))
        with self.assertRaises(ValueError):
            self.page(['tt2'], 101, next_offset=120)
        self.assertEqual((tuple(self.cat()), self.proof(), self.members()), before)

    def test_stale_generation_cannot_authoritatively_erase_members(self):
        stale = self.cat()
        self.page(['tt1'], next_offset=20)
        self.assertFalse(self.store.add_page(stale, [], 0, 100, complete=True, next_offset=0))
        self.assertEqual(self.members(), ['tt1'])

    def test_cursor_and_members_are_bounded_at_cap(self):
        self.store.add_page(self.cat(), [{'id': 'tt%d' % n} for n in range(20)], 0, 25, next_offset=20)
        self.store.add_page(self.cat(), [{'id': 'tt%d' % n} for n in range(20, 40)], 20, 25, next_offset=40)
        self.assertEqual((self.cat()['offset'], self.cat()['done']), (25, 1))
        self.assertEqual(len(self.members()), 25)
        self.assertEqual(max(r[0] for r in self.store.db.execute('SELECT rank FROM membership')), 24)

    def test_identity_cache_distinguishes_absence_expiry_and_kind(self):
        self.assertIsNone(self.store.get_tmdb_identity('movie', 42, 1000))
        self.store.set_tmdb_identity('movie', 42, 'tt0042', 2000)
        self.store.set_tmdb_identity('series', '42', None, 1500)
        self.assertEqual(self.store.get_tmdb_identity('movie', '42', 1000)['imdb_id'], 'tt0042')
        self.assertIsNone(self.store.get_tmdb_identity('series', 42, 1000)['imdb_id'])
        self.assertIsNone(self.store.get_tmdb_identity('series', 42, 1500))
        self.assertIsNone(self.store.get_tmdb_identity('movie', 42, 2000))

    def test_identities_persist_restart_and_policy_invalidation(self):
        self.store.set_tmdb_identity('movie', 42, 'tt0042', 2000)
        self.store.invalidate()
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_tmdb_identity('movie', 42, 1000)['imdb_id'], 'tt0042')

    def test_identity_validation_rejects_ambiguous_or_malformed_inputs(self):
        for kind, tmdb in [('tv', 42), ('movie', True), ('movie', 0), ('movie', 'tmdb:42'),
                           ('movie', 4.2), ('movie', '0042'), ('movie', 2 ** 64)]:
            with self.assertRaises(ValueError):
                self.store.set_tmdb_identity(kind, tmdb, 'tt42', 2000)
        for imdb in ('', '42', ['tt42'], 'tt42:1:1'):
            with self.assertRaises(ValueError):
                self.store.set_tmdb_identity('movie', 42, imdb, 2000)
        for expires in (0, -1, float('inf'), float('nan'), True, '2000'):
            with self.assertRaises(ValueError):
                self.store.set_tmdb_identity('movie', 42, 'tt42', expires)

    def test_seed_uses_only_unambiguous_pairs_without_resetting_confirmations(self):
        previews = [
            {'id': 'tt1', 'tmdbId': 101, 'imdbId': 'tt1'},
            {'id': 'tt2', 'tmdbId': 102, 'imdb_id': 'tt2'},
            {'id': 'tt3', 'tmdbId': 102, 'imdb_id': 'tt3'},  # Conflicting aliases.
            {'id': 'tt4', 'tmdbId': 104, 'imdbId': 'tt5'},  # Conflicting document.
            {'id': 'tt6', 'tmdbId': 106, 'tmdb_id': 107},  # Conflicting TMDB ids.
            {'id': 'tmdb:108', 'imdbId': 'tt8'},
            {'id': 'tt9', 'ProviderIds': {'Tmdb': '109'}},
        ]
        self.store.add_page(self.cat(), previews, 0, 100)
        self.store.record('movie', 'tt1', 'tt1', 'available', 1, 1000)
        proof = self.proof()
        self.assertEqual(self.store.seed_tmdb_identities(1000), 3)
        self.assertEqual(self.store.get_tmdb_identity('movie', 101, 1000)['imdb_id'], 'tt1')
        self.assertEqual(self.store.get_tmdb_identity('movie', 108, 1000)['imdb_id'], 'tt8')
        self.assertEqual(self.store.get_tmdb_identity('movie', 109, 1000)['imdb_id'], 'tt9')
        for tmdb in (102, 104, 106, 107):
            self.assertIsNone(self.store.get_tmdb_identity('movie', tmdb, 1000))
        before = self.store.get_tmdb_identity('movie', 101, 1000)
        self.assertEqual(self.store.seed_tmdb_identities(2000), 0)
        self.assertEqual(self.store.get_tmdb_identity('movie', 101, 1000), before)
        self.assertEqual(self.proof(), proof)

    def test_seed_does_not_override_existing_authoritative_absence(self):
        self.store.add_page(self.cat(), [{'id': 'tt1', 'tmdbId': 101}], 0, 100)
        self.store.set_tmdb_identity('movie', 101, None, 2000)
        self.assertEqual(self.store.seed_tmdb_identities(1000), 0)
        self.assertIsNone(self.store.get_tmdb_identity('movie', 101, 1000)['imdb_id'])


if __name__ == '__main__':
    unittest.main()
