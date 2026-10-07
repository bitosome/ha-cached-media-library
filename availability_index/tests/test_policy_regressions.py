import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import Store, playable, policy_fingerprint, stream_verdict


class PolicyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            'excludeUncached': True,
            'requiredStreamExpressions': [{'enabled': True, 'expression': "cached(service(streams, 'torbox'))"}],
            'presets': [{'type': 'torrentio', 'instanceId': 'provider', 'enabled': True,
                         'options': {'resources': ['stream'], 'providers': ['rutor']}}],
        }

    def test_all_eligibility_and_unknown_future_fields_invalidate(self):
        changes = {
            'requiredLanguages': ['Russian'], 'requiredResolutions': ['2160p'],
            'excludedAudioTags': ['DTS'], 'requiredEncodes': ['HEVC'],
            'size': {'global': {'minSize': 100}}, 'bitrate': {'global': {'maxSize': 50}},
            'excludedStreamExpressions': [{'expression': 'streams', 'enabled': True}],
            'yearMatching': {'enabled': True}, 'episodeTitleMatching': {'enabled': True},
            'serviceWrap': {'enabled': True}, 'futureEligibilitySetting': True,
            'services': [{'id': 'torbox', 'enabled': True, 'credential': 'changed'}],
        }
        original = policy_fingerprint(self.config)
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertNotEqual(original, policy_fingerprint(dict(self.config, **{key: value})))

    def test_provider_changes_invalidate(self):
        for mutate in (
            lambda preset: preset.update(enabled=False),
            lambda preset: preset['options'].update(providers=['rutracker']),
            lambda preset: preset['options'].update(manifestUrl='https://provider.invalid/changed/manifest.json'),
        ):
            changed = copy.deepcopy(self.config)
            mutate(changed['presets'][0])
            self.assertNotEqual(policy_fingerprint(self.config), policy_fingerprint(changed))

    def test_presentation_and_catalogue_switch_do_not_invalidate(self):
        changed = copy.deepcopy(self.config)
        changed.update(sortCriteria={'global': [{'key': 'language'}]},
                       formatter={'name': 'Readable labels', 'id': 'custom'},
                       addonName='New name', jellyfin={'maxLibraries': 40},
                       hideErrors=True, hideErrorsForResources=['stream'],
                       catalogModifications=[{'id': 'original', 'enabled': False}])
        changed['presets'].insert(0, {'type': 'custom', 'instanceId': 'cachedlibrary', 'enabled': True,
                                     'options': {'resources': ['catalog', 'meta'],
                                                 'manifestUrl': 'http://index.invalid/private/manifest.json'}})
        changed['presets'].append({'type': 'tmdb-addon', 'instanceId': 'metadata', 'enabled': True,
                                  'options': {'resources': [{'name': 'catalog'}, {'name': 'meta'}]}})
        self.assertEqual(policy_fingerprint(self.config), policy_fingerprint(changed))

    def test_mixed_provider_catalogue_resource_toggle_is_presentation_only(self):
        changed = copy.deepcopy(self.config)
        changed['presets'][0]['options']['resources'] = ['stream', 'catalog', 'meta']
        before = copy.deepcopy(changed)
        self.assertEqual(policy_fingerprint(self.config), policy_fingerprint(changed))
        self.assertEqual(changed, before, 'fingerprinting must not mutate the active configuration')

    def test_parent_and_variants_are_not_ignored(self):
        for extra in ({'parentConfig': {'uuid': 'parent', 'password': 'p', 'mergeStrategies': {'filters': 'inherit'}}},
                      {'variants': [{'id': 'russian', 'enabled': True, 'script': 'set requiredLanguages = ["Russian"]'}]}):
            changed = dict(self.config, **extra)
            self.assertNotEqual(policy_fingerprint(self.config), policy_fingerprint(changed))
        variant = dict(self.config, variants=[{'id': 'russian', 'name': 'Old label', 'script': 'script'}])
        renamed = copy.deepcopy(variant)
        renamed['variants'][0]['name'] = 'New label'
        self.assertEqual(policy_fingerprint(variant), policy_fingerprint(renamed))


class StreamVerdictRegressionTests(unittest.TestCase):
    def test_hidden_errors_and_empty_results_are_unknown(self):
        for result in ({'streams': []}, {'streams': [{'infoHash': 'abc'}]},
                       {'streams': [{'externalUrl': 'https://example.invalid'}]},
                       {'streams': [None, 'malformed']}, None, {}):
            with self.subTest(result=result):
                self.assertEqual(stream_verdict(result), ('error', 0))

    def test_only_explicit_filtering_report_is_a_negative(self):
        removed = {'name': '🔍 Removal Reasons', 'description': '📌 Excluded Uncached (8)',
                   'streamData': {'type': 'statistic'}}
        self.assertEqual(stream_verdict({'streams': [removed]}), ('unavailable', 0))
        self.assertEqual(stream_verdict({'streams': [removed], 'errors': ['provider failed']}), ('error', 0))
        self.assertEqual(stream_verdict({'streams': [removed, {'name': 'Provider timeout'}]}), ('error', 0))
        self.assertEqual(stream_verdict({'streams': [dict(removed, description='Excluded Uncached (0)')]}), ('error', 0))

    def test_notice_videos_and_explicit_uncached_streams_never_count(self):
        url = 'https://example.invalid/video.mp4'
        for stream in (
            {'url': url, 'streamData': {'type': 'info'}},
            {'url': url, 'streamData': {'type': 'error'}},
            {'url': url, 'streamData': {'type': 'statistic'}},
            {'url': url, 'streamData': {'error': {'title': 'Failure'}}},
            {'url': url, 'streamData': {'id': 'error.notice'}},
            {'url': url, 'id': 'aiostreamserror.notice'},
            {'url': 'https://example.invalid/aiostreamserror.notice'},
            {'url': url, 'streamData': {'service': {'id': 'torbox', 'cached': False}}},
            {'url': 'https://'}, {'url': 'http://[malformed'}, {'url': 123},
        ):
            with self.subTest(stream=stream):
                self.assertFalse(playable(stream))
                self.assertNotEqual(stream_verdict({'streams': [stream]})[0], 'available')

    def test_real_stream_wins_even_when_another_provider_failed(self):
        # Do not mistake a film title or arbitrary CDN path containing "error"
        # for an AIOStreams information card.
        real = {'url': 'https://cdn.invalid/error-in-the-movie-name.mkv',
                'streamData': {'type': 'debrid', 'service': {'id': 'torbox', 'cached': True}}}
        self.assertEqual(stream_verdict({'streams': [real, {'name': 'Provider timeout'}]}), ('available', 1))

    def test_ambiguous_refresh_preserves_positive_without_extending_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory + '/db', positive=3600)
            store.add_categories([{'id': 'movies', 'name': 'Movies', 'type': 'movie'}], 'cachedlibrary')
            category = store.db.execute('SELECT * FROM categories').fetchone()
            store.add_page(category, [{'id': 'tt1', 'name': 'One'}], 0, 100)
            store.record('movie', 'tt1', 'tt1', 'available', 1, 1000)
            verdict, count = stream_verdict({'streams': []})
            store.record('movie', 'tt1', 'tt1', verdict, count, 1100)
            row = store.db.execute('SELECT * FROM checks').fetchone()
            self.assertEqual(row['status'], 'available')
            self.assertEqual(row['expires'], 4600)
            self.assertLess(row['due'], 4700)
            store.db.close()


if __name__ == '__main__':
    unittest.main()
