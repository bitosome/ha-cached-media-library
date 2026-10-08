import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store


class AliasEvidenceTests(unittest.TestCase):
    NOW = 10000
    POLICY = 'same-effective-policy'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name + '/db', positive=3600, negative=7200)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.db.close)
        self.store.add_categories([{'id': 'upstream.shows', 'type': 'series', 'name': 'Shows'}], 'cachedlibrary')
        self.cat = self.store.db.execute('SELECT * FROM categories').fetchone()
        self.store.add_page(self.cat, [{'id': 'tt1', 'name': 'A'}, {'id': 'tmdb:1', 'name': 'A translated'}], 0, 100)
        for parent in ('tt1', 'tmdb:1'):
            self.store.save_meta('series', parent, self.meta(), self.NOW)
        self.store.setting('policy', self.POLICY)

    def meta(self, canonical='tt1', ids=None):
        return {'id': canonical, 'name': 'A', 'videos': [
            {'id': ident, 'season': 1, 'episode': i + 1, 'released': '1970-01-01T00:00:00Z'}
            for i, ident in enumerate(ids or ['tt1:1:1', 'tt1:1:2'])]}

    def row(self, parent, ident='tt1:1:1'):
        row = self.store.db.execute('SELECT * FROM checks WHERE type=? AND id=? AND parent=?', ('series', ident, parent)).fetchone()
        return dict(row) if row else None

    def seed(self, parent, ident='tt1:1:1', status='available', checked=9900, expiry=None):
        ttl = self.store.positive if status == 'available' else self.store.negative
        self.store.db.execute('''UPDATE checks SET status=?,checked=?,expires=?,due=?,count=?
          WHERE type='series' AND id=? AND parent=?''',
          (status, checked, expiry if expiry is not None else checked + ttl, checked + ttl * .8,
           3 if status == 'available' else 0, ident, parent))
        self.store.db.commit()

    def test_live_result_is_shared_with_same_episode_and_canonical_parent(self):
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 3, self.NOW)
        first, alias = self.row('tt1'), self.row('tmdb:1')
        for key in ('status', 'checked', 'expires', 'count'):
            self.assertEqual(alias[key], first[key])
        self.assertEqual(alias['expires'], self.NOW + 3600)
        self.assertIsNone(self.store.meta('series', 'tmdb:1', self.NOW + 3601))

    def test_existing_split_proofs_repair_the_selected_catalogue_alias_union(self):
        self.seed('tt1', 'tt1:1:1', checked=9800)
        self.seed('tmdb:1', 'tt1:1:2', checked=9900)
        repaired = self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(repaired['shared'], 2)
        shelf = self.store.catalog('series', self.cat['id'], now=self.NOW)
        self.assertEqual(len(shelf), 1)
        selected = self.store.meta('series', shelf[0]['id'], self.NOW)
        self.assertEqual({v['id'] for v in selected['videos']}, {'tt1:1:1', 'tt1:1:2'})
        self.assertEqual(self.row('tmdb:1')['checked'], 9800)
        self.assertEqual(self.row('tmdb:1')['expires'], 13400)

    def test_new_alias_metadata_gets_existing_evidence_without_rechecking(self):
        self.store.db.execute("DELETE FROM checks WHERE parent='tmdb:1'")
        self.store.db.commit()
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 2, 9800)
        self.store.save_meta('series', 'tmdb:1', self.meta(), self.NOW)
        self.assertEqual(self.row('tmdb:1')['checked'], 9800)
        self.assertEqual(self.row('tmdb:1')['expires'], 13400)
        self.assertEqual(self.row('tmdb:1')['status'], 'available')

    def test_newer_negative_wins_over_older_positive(self):
        self.seed('tt1', status='available', checked=9800)
        self.seed('tmdb:1', status='unavailable', checked=9900)
        self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(self.row('tt1')['status'], 'unavailable')
        self.assertEqual(self.row('tt1')['checked'], 9900)
        self.assertFalse(self.store.visible('series', 'tt1', self.NOW))

    def test_out_of_order_positive_cannot_resurrect_newer_negative_alias(self):
        self.seed('tmdb:1', status='unavailable', checked=9900)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 3, 9800)
        self.assertEqual(self.row('tt1')['status'], 'unavailable')
        # The source's timestamp is earlier than another alias's conclusion.
        # Reconciliation at current time must not treat that older proof as new.
        self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'unavailable')
        self.assertEqual(self.row('tt1')['status'], 'unavailable')

    def test_negative_wins_contradictory_equal_timestamp_proofs(self):
        self.seed('tt1', status='available', checked=9900)
        self.seed('tmdb:1', status='unavailable', checked=9900)
        self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(self.row('tt1')['status'], 'unavailable')

    def test_newer_pending_timestamp_is_not_overwritten(self):
        self.seed('tt1', checked=9800)
        self.store.db.execute("UPDATE checks SET checked=9900 WHERE parent='tmdb:1' AND id='tt1:1:1'")
        self.store.db.commit()
        self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')
        self.assertEqual(self.row('tmdb:1')['checked'], 9900)

    def test_same_episode_string_in_different_shows_does_not_share(self):
        self.store.save_meta('series', 'tmdb:1', self.meta(canonical='tt2'), self.NOW)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')

    def test_same_show_but_different_episode_id_does_not_share(self):
        self.store.save_meta('series', 'tmdb:1', self.meta(ids=['tmdb:1:1:1']), self.NOW)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.NOW)
        self.assertEqual(self.row('tmdb:1', 'tmdb:1:1:1')['status'], 'pending')

    def test_conflicting_explicit_parent_identifiers_prevent_merging(self):
        self.store.save_meta('series', 'tt1', self.meta(canonical='tt2'), self.NOW)
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')

    def test_inactive_alias_is_not_changed(self):
        self.store.db.execute("DELETE FROM membership WHERE id='tmdb:1'")
        self.store.db.commit()
        self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')

    def test_expired_evidence_is_not_made_fresh(self):
        self.seed('tt1', checked=6000)
        self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')
        self.assertFalse(self.store.visible('series', 'tt1', self.NOW))
        self.store.save_meta('series', 'tmdb:1', self.meta(), self.NOW)
        self.assertFalse(self.store.visible('series', 'tmdb:1', self.NOW))
        self.assertLessEqual(self.row('tmdb:1')['expires'], 9600)

    def test_policy_mismatch_and_invalidation_block_old_alias_evidence(self):
        self.seed('tt1')
        with self.assertRaises(ValueError):
            self.store.reconcile_alias_confirmations('different-policy', self.NOW)
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')
        self.store.invalidate()
        self.store.setting('policy', 'new-policy')
        self.assertEqual(self.store.reconcile_alias_confirmations('new-policy', self.NOW)['shared'], 0)
        self.assertFalse(self.store.visible('series', 'tt1', self.NOW))

    def test_same_policy_bulk_repair_runs_only_once(self):
        self.seed('tt1')
        self.store.reconcile_alias_confirmations(self.POLICY, self.NOW)
        self.assertEqual(self.store.reconcile_alias_confirmations(self.POLICY, self.NOW + 1),
                         {'shared': 0, 'already_reconciled': True})

    def test_failed_alias_update_rolls_back_original_record(self):
        self.store.db.execute('''CREATE TRIGGER reject_alias BEFORE UPDATE ON checks
          WHEN NEW.parent='tmdb:1' BEGIN SELECT RAISE(ABORT,'test alias rollback'); END''')
        self.store.db.commit()
        with self.assertRaises(Exception):
            self.store.record('series', 'tt1:1:1', 'tt1', 'available', 1, self.NOW)
        self.assertEqual(self.row('tt1')['status'], 'pending')
        self.assertEqual(self.row('tmdb:1')['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
