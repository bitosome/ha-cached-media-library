import copy
import base64
import json
import os
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import App, Store, policy_fingerprint


class RecoveryTests(unittest.TestCase):
    NOW = 10000

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name + '/db', positive=3600)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.db.close)
        self.store.add_categories([{'id': 'original.movies', 'name': 'Movies', 'type': 'movie'}], 'cachedlibrary')
        self.category = self.store.db.execute('SELECT * FROM categories').fetchone()
        self.store.add_page(self.category, [{'id': 'tt1', 'name': 'One'}], 0, 100)
        self.store.record('movie', 'tt1', 'tt1', 'available', 3, 9000)
        row = self.row()
        self.snapshot = {'version': 1, 'policy': 'equivalent-policy', 'checks': [
            {key: row[key] for key in ('type', 'id', 'parent', 'checked', 'expires', 'due', 'count')}],
        }
        self.store.invalidate()

    def row(self):
        return dict(self.store.db.execute("SELECT * FROM checks WHERE id='tt1'").fetchone())

    def restore(self, snapshot=None, digest='digest-1'):
        return self.store.recover_confirmations(snapshot or self.snapshot, 'equivalent-policy', digest, now=self.NOW)

    def test_invalidated_confirmation_recovers_original_time_and_expiry(self):
        result = self.restore()
        self.assertEqual(result['restored'], 1)
        row = self.row()
        self.assertEqual(row['status'], 'available')
        self.assertEqual(row['checked'], 9000)
        self.assertEqual(row['expires'], 12600)
        self.assertEqual(row['count'], 3)
        self.assertTrue(self.store.visible('movie', 'tt1', self.NOW))

    def test_newer_error_attempt_does_not_destroy_earlier_confirmation(self):
        self.store.record('movie', 'tt1', 'tt1', 'error', 0, self.NOW - 1)
        before = self.row()
        self.assertGreater(before['attempted'], self.snapshot['checks'][0]['checked'])
        self.assertEqual(self.restore()['restored'], 1)
        after = self.row()
        self.assertEqual(after['attempted'], before['attempted'])
        self.assertEqual(after['failures'], before['failures'])
        self.assertEqual(after['expires'], 12600)

    def test_newer_conclusive_verdict_is_never_overwritten(self):
        for verdict in ('available', 'unavailable'):
            with self.subTest(verdict=verdict):
                self.store.record('movie', 'tt1', 'tt1', verdict, 1 if verdict == 'available' else 0, 9500)
                before = self.row()
                self.assertEqual(self.restore(digest='conclusive-' + verdict)['restored'], 0)
                self.assertEqual(self.row(), before)

    def test_newer_conclusive_time_survives_even_if_later_invalidated(self):
        self.store.record('movie', 'tt1', 'tt1', 'unavailable', 0, 9500)
        self.store.invalidate()
        self.assertEqual(self.row()['status'], 'pending')
        self.assertEqual(self.restore()['restored'], 0)
        self.assertFalse(self.store.visible('movie', 'tt1', self.NOW))

    def test_recovery_never_overwrites_existing_conclusive_rows(self):
        # Even an older negative is retained by the deliberately conservative
        # pending-only import; recovery is not a second authority for live checks.
        # Seed historical state directly: record() correctly refuses to replace
        # the fixture's newer checked=9000 timestamp with an older live result.
        self.store.db.execute("UPDATE checks SET status='unavailable',checked=8500,expires=15700,count=0 WHERE id='tt1'")
        self.store.db.commit()
        self.assertEqual(self.restore()['restored'], 0)
        self.assertEqual(self.row()['status'], 'unavailable')

    def test_expired_and_immediately_expiring_evidence_is_not_published(self):
        for expiry in (self.NOW - 1, self.NOW + 60):
            with self.subTest(expiry=expiry):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['checks'][0]['expires'] = expiry
                self.assertEqual(self.restore(snapshot, digest=str(expiry))['restored'], 0)
        self.assertEqual(self.row()['status'], 'pending')

    def test_current_shorter_positive_ttl_limits_recovered_expiry(self):
        self.store.positive = 1500
        self.assertEqual(self.restore()['restored'], 1)
        self.assertEqual(self.row()['expires'], 10500)
        self.assertLessEqual(self.row()['due'], 10440)

    def test_shortened_ttl_can_make_snapshot_too_old_to_restore(self):
        self.store.positive = 1000
        self.assertEqual(self.restore()['restored'], 0)
        self.assertEqual(self.row()['status'], 'pending')

    def test_only_existing_active_checks_can_be_restored(self):
        missing = copy.deepcopy(self.snapshot['checks'][0])
        missing['id'] = missing['parent'] = 'tt-missing'
        snapshot = dict(self.snapshot, checks=[missing])
        self.assertEqual(self.restore(snapshot, digest='missing')['restored'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 1)
        self.store.db.execute('UPDATE categories SET active=0')
        self.store.db.commit()
        self.assertEqual(self.restore(digest='inactive')['restored'], 0)

    def test_digest_prevents_reapplication_after_later_invalidation(self):
        self.assertEqual(self.restore()['restored'], 1)
        self.store.invalidate()
        self.assertEqual(self.restore(), {'restored': 0, 'already_applied': True})
        self.assertEqual(self.row()['status'], 'pending')

    def test_policy_mismatch_is_rejected_before_any_write(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['policy'] = 'different-filters'
        with self.assertRaises(ValueError):
            self.restore(snapshot)
        self.assertEqual(self.row()['status'], 'pending')
        self.assertIsNone(self.store.setting('applied_recovery_snapshots'))

    def test_malformed_envelopes_and_excessive_record_count_are_rejected(self):
        for snapshot in ([], {'version': 2, 'policy': 'equivalent-policy', 'checks': []},
                         {'version': 1, 'policy': 'equivalent-policy', 'checks': None},
                         {'version': 1, 'policy': 'equivalent-policy', 'checks': [None] * 100001}):
            with self.subTest(kind=type(snapshot).__name__):
                with self.assertRaises(ValueError):
                    self.store.recover_confirmations(snapshot, 'equivalent-policy', 'bad', now=self.NOW)
                self.assertEqual(self.row()['status'], 'pending')
                self.assertIsNone(self.store.setting('applied_recovery_snapshots'))

    def test_bad_timestamps_counts_and_identifiers_are_rejected_atomically(self):
        mutations = [
            ('checked', 0), ('checked', self.NOW + 1), ('checked', True),
            ('checked', float('nan')), ('expires', float('inf')), ('due', float('-inf')),
            ('expires', 8999), ('due', 8999), ('count', 0), ('count', True),
            ('count', 1.5), ('id', ''), ('parent', 'x' * 513), ('type', 'anime'),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                snapshot = copy.deepcopy(self.snapshot)
                invalid = copy.deepcopy(snapshot['checks'][0])
                invalid[field] = value
                # The valid record precedes the malformed one: validation must
                # finish before recovery writes anything or marks the digest.
                snapshot['checks'].append(invalid)
                with self.assertRaises(ValueError):
                    self.restore(snapshot)
                self.assertEqual(self.row()['status'], 'pending')
                self.assertIsNone(self.store.setting('applied_recovery_snapshots'))

    def test_failed_transaction_does_not_mark_digest_or_restore_partial_rows(self):
        self.store.db.execute('''CREATE TRIGGER prevent_recovery_marker BEFORE INSERT ON settings
          WHEN NEW.key='last_recovery' BEGIN SELECT RAISE(ABORT,'test rollback'); END''')
        self.store.db.commit()
        with self.assertRaises(Exception):
            self.restore()
        self.assertEqual(self.row()['status'], 'pending')
        self.assertIsNone(self.store.setting('applied_recovery_snapshots'))


class RecoveryDecoderTests(unittest.TestCase):
    def test_malformed_or_oversized_compressed_payload_never_applies(self):
        config = {'excludeUncached': True, 'requiredStreamExpressions': [
            {'enabled': True, 'expression': "cached(service(streams, 'torbox'))"}]}
        envelope = json.dumps({'version': 1, 'policy': policy_fingerprint(config), 'checks': []}).encode()
        compressed = zlib.compress(envelope)
        cases = [
            ('invalid-base64', 'not valid base64!'),
            ('invalid-zlib', base64.b64encode(b'not zlib').decode()),
            ('truncated-zlib', base64.b64encode(compressed[:-3]).decode()),
            ('trailing-zlib-data', base64.b64encode(compressed + b'trailing').decode()),
            ('invalid-json', base64.b64encode(zlib.compress(b'not json')).decode()),
            ('compressed-bomb', base64.b64encode(zlib.compress(b' ' * (16 * 1024 * 1024 + 1))).decode()),
            ('encoded-oversize', 'A' * (4 * 1024 * 1024 + 1)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for label, payload in cases:
                with self.subTest(label=label):
                    app = App({'aiostreams_url': 'http://aio.invalid:3000',
                               'active_uuid': 'family', 'active_password': 'password',
                               'stremio_uuid': 'family', 'stremio_encrypted_password': 'encrypted',
                               'catalog_uuid': 'original', 'catalog_encrypted_password': 'catalog-encrypted',
                               'endpoint_token': 'test-token-with-at-least-24-characters',
                               'recovery_snapshot': payload}, directory + '/' + label)

                    def request(path, **kwargs):
                        if kwargs.get('config'):
                            return {'data': {'userData': config}}
                        return {'catalogs': [{'id': 'original.movies', 'type': 'movie', 'name': 'Movies'}]}

                    app.request = request
                    try:
                        with self.assertRaises(ValueError):
                            app.synchronize()
                        self.assertIsNone(app.store.setting('applied_recovery_snapshots'))
                        self.assertFalse(app.ready)
                    finally:
                        app.store.db.close()


if __name__ == '__main__':
    unittest.main()
