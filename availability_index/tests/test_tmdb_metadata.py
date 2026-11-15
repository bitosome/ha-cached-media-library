"""Title and episode metadata must come straight from TMDB, safely."""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import renumbered, App, Store, error_meta
from tmdb_metadata import TMDBClient, TMDBError, TMDBMetadata, TMDBNotFound


def tv_details(**extra):
    return {'id': 82728, 'name': 'Bluey', 'original_name': 'Bluey', 'overview': 'A puppy.',
            'first_air_date': '2018-10-01', 'last_air_date': '2024-04-21', 'status': 'Returning Series',
            'episode_run_time': [7], 'vote_average': 8.6, 'genres': [{'id': 16, 'name': 'Animation'}],
            'poster_path': '/poster.jpg', 'backdrop_path': '/back.jpg',
            'external_ids': {'imdb_id': 'tt7678620'},
            'content_ratings': {'results': [{'iso_3166_1': 'US', 'rating': 'TV-Y'}]},
            'images': {'logos': [{'file_path': '/logo.png'}]},
            'credits': {'cast': [{'name': 'David McCormack'}], 'crew': []},
            'created_by': [{'name': 'Joe Brumm'}],
            'seasons': [{'season_number': 0}, {'season_number': 1}], **extra}


def season(number, episodes):
    return {'season_number': number, 'episodes': [
        {'episode_number': n, 'name': 'Episode ' + str(n), 'overview': 'Plot', 'air_date': aired,
         'still_path': '/still.jpg', 'vote_average': 8.0, 'runtime': 7} for n, aired in episodes]}


class Transport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        key = (path, params.get('append_to_response', '').startswith('season/'))
        value = self.routes.get(key, self.routes.get(path))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise TMDBNotFound('TMDB has no such title')
        return value


class TMDBMetadataTests(unittest.TestCase):
    def series_routes(self):
        return {('/tv/82728', False): tv_details(),
                ('/tv/82728', True): {'id': 82728,
                                      'season/0': season(0, [(1, '2019-12-01')]),
                                      'season/1': season(1, [(1, '2018-10-01'), (2, '2018-10-02'), (3, None)])}}

    def test_series_keeps_stream_check_episode_identities(self):
        transport = Transport(self.series_routes())
        meta, tmdb_id = TMDBMetadata(transport).meta('series', 'tt7678620', 82728)
        self.assertEqual(tmdb_id, 82728)
        self.assertEqual([v['id'] for v in meta['videos']],
                         ['tt7678620:0:1', 'tt7678620:1:1', 'tt7678620:1:2', 'tt7678620:1:3'])
        self.assertEqual(meta['videos'][1]['released'], '2018-10-01T00:00:00.000Z')
        self.assertNotIn('released', meta['videos'][3])  # No air date: stays unreleased.
        self.assertEqual((meta['id'], meta['imdb_id'], meta['tmdb_id']), ('tt7678620', 'tt7678620', '82728'))
        self.assertEqual((meta['releaseInfo'], meta['ageRating'], meta['runtime']), ('2018-', 'TV-Y', '7min'))
        self.assertEqual(meta['logo'], 'https://image.tmdb.org/t/p/original/logo.png')
        self.assertEqual(meta['writer'], ['Joe Brumm'])
        self.assertTrue(all(path.startswith(('/tv/', '/find/')) for path, _ in transport.calls))

    def test_tmdb_only_title_uses_its_own_identity(self):
        routes = self.series_routes()
        routes[('/tv/82728', False)] = tv_details(external_ids={'imdb_id': None})
        meta, _ = TMDBMetadata(Transport(routes)).meta('series', 'tmdb:82728')
        self.assertEqual(meta['id'], 'tmdb:82728')
        self.assertNotIn('imdb_id', meta)
        self.assertEqual(meta['videos'][1]['id'], 'tmdb:82728:1:1')

    def test_wrong_hint_is_resolved_through_the_imdb_identity(self):
        routes = self.series_routes()
        routes[('/tv/5', False)] = tv_details(id=5, external_ids={'imdb_id': 'tt0000005'}, seasons=[])
        routes['/find/tt7678620'] = {'movie_results': [], 'tv_results': [{'id': 82728}]}
        meta, tmdb_id = TMDBMetadata(Transport(routes)).meta('series', 'tt7678620', 5)
        self.assertEqual((tmdb_id, meta['name']), (82728, 'Bluey'))

    def test_unknown_imdb_identity_is_authoritative_absence(self):
        routes = {'/find/tt7678620': {'movie_results': [], 'tv_results': []}}
        with self.assertRaises(TMDBNotFound):
            TMDBMetadata(Transport(routes)).meta('series', 'tt7678620')
        with self.assertRaises(TMDBNotFound):
            TMDBMetadata(Transport({})).meta('movie', 'kitsu:1')

    def test_truncated_season_is_an_error_not_an_empty_show(self):
        routes = self.series_routes()
        del routes[('/tv/82728', True)]['season/1']
        with self.assertRaises(TMDBError) as caught:
            TMDBMetadata(Transport(routes)).meta('series', 'tt7678620', 82728)
        self.assertNotIsInstance(caught.exception, TMDBNotFound)

    def test_many_seasons_are_requested_in_bounded_chunks(self):
        numbers = list(range(1, 46))
        details = tv_details(seasons=[{'season_number': n} for n in numbers])
        seasons = {'id': 82728, **{'season/' + str(n): season(n, [(1, '2020-01-01')]) for n in numbers}}
        transport = Transport({('/tv/82728', False): details, ('/tv/82728', True): seasons})
        meta, _ = TMDBMetadata(transport).meta('series', 'tt7678620', 82728)
        self.assertEqual(len(meta['videos']), 45)
        chunks = [p['append_to_response'].count('season/') for _, p in transport.calls
                  if p.get('append_to_response', '').startswith('season/')]
        self.assertEqual(chunks, [20, 20, 5])

    def test_movie_metadata(self):
        details = {'id': 157336, 'title': 'Interstellar', 'overview': 'Space.', 'release_date': '2014-11-05',
                   'runtime': 169, 'vote_average': 8.46, 'genres': [{'name': 'Adventure'}],
                   'poster_path': '/p.jpg', 'imdb_id': 'tt0816692', 'external_ids': {'imdb_id': 'tt0816692'},
                   'release_dates': {'results': [{'iso_3166_1': 'US', 'release_dates': [
                       {'certification': ''}, {'certification': 'PG-13'}]}]},
                   'credits': {'cast': [], 'crew': [{'name': 'Christopher Nolan', 'job': 'Director'}]}}
        routes = {'/find/tt0816692': {'movie_results': [{'id': 157336}], 'tv_results': []},
                  '/movie/157336': details}
        meta, tmdb_id = TMDBMetadata(Transport(routes)).meta('movie', 'tt0816692')
        self.assertEqual(tmdb_id, 157336)
        self.assertEqual((meta['runtime'], meta['releaseInfo'], meta['imdbRating'], meta['ageRating']),
                         ('2h49min', '2014', '8.5', 'PG-13'))
        self.assertEqual(meta['director'], ['Christopher Nolan'])
        self.assertNotIn('videos', meta)

    def test_untrusted_image_paths_are_dropped(self):
        details = tv_details(poster_path='https://evil.invalid/x.jpg', backdrop_path='/../x.jpg', seasons=[])
        meta, _ = TMDBMetadata(Transport({('/tv/82728', False): details})).meta('series', 'tmdb:82728')
        self.assertNotIn('poster', meta)
        self.assertNotIn('background', meta)


class TMDBClientTests(unittest.TestCase):
    def test_v3_key_is_sent_as_query_and_never_as_bearer(self):
        client = TMDBClient('a' * 32, interval=0)
        client._opener = Mock()
        client._opener.open.return_value = io.StringIO('{"id": 1}')
        client.fetch('/movie/1', {'language': 'en-US'})
        request = client._opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://api.themoviedb.org/3/movie/1?'))
        self.assertIn('api_key=' + 'a' * 32, request.full_url)
        self.assertIsNone(request.get_header('Authorization'))

    def test_errors_never_repeat_the_credential_or_url(self):
        client = TMDBClient('secret-token-value', interval=0)
        client._opener = Mock()
        for failure, kind in ((HTTPError('https://x/?api_key=secret-token-value', 404, 'nf', {}, None), TMDBNotFound),
                              (HTTPError('https://x/?api_key=secret-token-value', 500, 'boom', {}, None), TMDBError),
                              (OSError('secret-token-value'), TMDBError)):
            client._opener.open.side_effect = failure
            with self.assertRaises(kind) as caught:
                client.fetch('/movie/1', {})
            self.assertNotIn('secret', str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_status_34_body_is_not_found(self):
        client = TMDBClient('a' * 32, request_json=lambda path, params: {'success': False, 'status_code': 34})
        with self.assertRaises(TMDBNotFound):
            client.fetch('/movie/1', {})


class NativeMetadataStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + '/db', positive=3600)
        self.now = time.time()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.db.close)
        self.store.add_categories([{'id': 'series', 'type': 'series', 'name': 'Series'},
                                   {'id': 'movies', 'type': 'movie', 'name': 'Movies'}], 'scanner')

    def add(self, kind, preview):
        cat = self.store.db.execute('SELECT * FROM categories WHERE type=?', (kind,)).fetchone()
        self.store.add_page(cat, [preview], 0, 100)

    def videos(self, prefix, count=5):
        return [{'id': prefix + ':1:' + str(n), 'season': 1, 'episode': n, 'released': '2018-10-01T00:00:00.000Z'}
                for n in range(1, count + 1)]

    def test_provider_error_card_never_replaces_metadata(self):
        self.add('movie', {'id': 'tt0816692', 'name': 'Interstellar'})
        good = {'id': 'tt0816692', 'type': 'movie', 'name': 'Interstellar', 'description': 'Space.'}
        self.store.save_meta('movie', 'tt0816692', good, self.now)
        card = {'id': 'tt0816692', 'type': 'movie', 'name': '[❌] The Movie Database', 'description': 'fetch failed'}
        self.assertTrue(error_meta(card))
        self.assertFalse(error_meta(good))
        self.store.save_meta('movie', 'tt0816692', card, self.now + 10)
        row = self.store.db.execute('SELECT meta,meta_due FROM titles WHERE id=?', ('tt0816692',)).fetchone()
        self.assertIn('Space.', row['meta'])
        self.assertAlmostEqual(row['meta_due'], self.now + 10 + 1800, delta=1)

    def test_stored_error_card_is_hidden_and_cleared_once(self):
        self.add('movie', {'id': 'tt0816692', 'name': 'Interstellar', 'tmdbId': 157336})
        self.store.record('movie', 'tt0816692', 'tt0816692', 'available', 1, self.now)
        self.store.db.execute("UPDATE titles SET meta=?,meta_due=?,meta_checked=? WHERE id='tt0816692'",
                              ('{"id":"tt0816692","name":"[\\u274c] The Movie Database","description":"fetch failed"}',
                               self.now + 9999, self.now))
        self.store.db.commit()
        self.assertEqual(self.store.meta('movie', 'tt0816692')['name'], 'Interstellar')
        self.assertEqual(self.store.catalog('movie', self.store.db.execute(
            "SELECT id FROM categories WHERE type='movie'").fetchone()['id'])[0].get('description'), None)
        self.assertEqual(self.store.tmdb_hint('movie', 'tt0816692'), 157336)
        self.assertEqual(self.store.refresh_all_metadata('marker'), 1)
        row = self.store.db.execute("SELECT meta,meta_due,meta_checked FROM titles WHERE id='tt0816692'").fetchone()
        self.assertEqual((row['meta'], row['meta_due'], row['meta_checked']), (None, 0, 0))
        self.assertEqual(self.store.refresh_all_metadata('marker'), 0)  # One-time only.

    def test_disjoint_episode_identities_do_not_discard_confirmations(self):
        self.add('series', {'id': 'tt7678620', 'name': 'Bluey'})
        self.store.save_meta('series', 'tt7678620', {'id': 'tt7678620', 'name': 'Bluey',
                                                     'videos': self.videos('tt7678620')}, self.now)
        self.store.record('series', 'tt7678620:1:1', 'tt7678620', 'available', 2, self.now)
        before = [tuple(r) for r in self.store.db.execute('SELECT * FROM checks ORDER BY id')]
        foreign = {'id': 'tt7678620', 'name': 'Bluey', 'videos': self.videos('tmdb:82728')}
        self.store.save_meta('series', 'tt7678620', foreign, self.now + 5, keep_identity=True)
        self.assertEqual(before, [tuple(r) for r in self.store.db.execute('SELECT * FROM checks ORDER BY id')])
        self.assertEqual(self.store.identity_conflicts, {('series', 'tt7678620')})
        grown = {'id': 'tt7678620', 'name': 'Bluey', 'videos': self.videos('tt7678620', 7)}
        self.store.save_meta('series', 'tt7678620', grown, self.now + 6, keep_identity=True)
        self.assertEqual(self.store.identity_conflicts, set())  # Resolved once a list matches.
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM checks').fetchone()[0], 7)
        self.assertEqual(self.store.db.execute(
            "SELECT status FROM checks WHERE id='tt7678620:1:1'").fetchone()[0], 'available')


    def test_other_season_layout_keeps_existing_episodes_and_confirmations(self):
        # Existing IMDb-style layout: two seasons of 8. TMDB offers one season of 16.
        old = self.videos('tt0388629', 8) + [dict(v, id=v['id'].replace(':1:', ':2:'), season=2)
                                              for v in self.videos('tt0388629', 8)]
        self.add('series', {'id': 'tt0388629', 'name': 'One Piece'})
        self.store.save_meta('series', 'tt0388629', {'id': 'tt0388629', 'name': 'One Piece', 'videos': old}, self.now)
        self.store.record('series', 'tt0388629:2:3', 'tt0388629', 'available', 2, self.now)
        before = [tuple(r) for r in self.store.db.execute('SELECT * FROM checks ORDER BY id')]
        stored = self.store.db.execute('SELECT meta FROM titles').fetchone()['meta']
        flat = {'id': 'tt0388629', 'name': 'One Piece', 'videos': self.videos('tt0388629', 16)}
        self.store.save_meta('series', 'tt0388629', flat, self.now + 5, keep_identity=True)
        self.assertEqual(before, [tuple(r) for r in self.store.db.execute('SELECT * FROM checks ORDER BY id')])
        row = self.store.db.execute('SELECT meta,meta_due FROM titles').fetchone()
        self.assertEqual(row['meta'], stored)
        self.assertGreater(row['meta_due'], self.now + 80000)  # Daily, not every 30 minutes.
        self.assertEqual(self.store.identity_conflicts, {('series', 'tt0388629')})

    def test_a_few_removed_episodes_and_changed_specials_are_an_ordinary_update(self):
        self.assertFalse(renumbered({'a:1:%d' % n for n in range(1, 41)} | {'a:0:1', 'a:0:2', 'a:0:3', 'a:0:4', 'a:0:5'},
                                    {'a:1:%d' % n for n in range(1, 38)}))   # 3 of 40 gone, specials gone
        self.assertTrue(renumbered({'a:1:%d' % n for n in range(1, 41)}, {'a:1:%d' % n for n in range(1, 36)}))
        self.assertFalse(renumbered({'a:1:1', 'a:1:2'}, {'b:1:1'}))            # Too little evidence.
        self.assertTrue(renumbered({'a:0:%d' % n for n in range(1, 6)}, {'b:0:1'}))


class NativeMetadataAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        options = {'aiostreams_url': 'http://aio.invalid', 'active_uuid': 'active', 'active_password': 'p',
                   'stremio_uuid': 'active', 'stremio_encrypted_password': 'e', 'catalog_uuid': 'source',
                   'catalog_encrypted_password': 'e', 'tmdb_api_key': 'a' * 32}
        self.app = App(options, self.temp.name + '/db')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.app.store.db.close)
        self.addCleanup(self.app.stop.set)
        self.app.request = lambda path, **kwargs: self.fail('Metadata must not request AIOStreams: ' + path)
        self.app.store.add_categories([{'id': 'series', 'type': 'series', 'name': 'Series'}], 'scanner')
        cat = self.app.store.db.execute('SELECT * FROM categories').fetchone()
        self.app.store.add_page(cat, [{'id': 'tt7678620', 'name': 'Bluey', 'tmdbId': 82728}], 0, 100)

    def test_metadata_comes_from_tmdb_without_the_source_profile(self):
        self.assertIsNotNone(self.app.tmdb_meta)
        self.assertIsNone(self.app.tmdb)  # No local catalogues are needed for metadata.
        transport = Transport(TMDBMetadataTests().series_routes())
        self.app.tmdb_meta._fetch = transport
        self.app.fetch_meta('series', 'tt7678620')
        ids = [r['id'] for r in self.app.store.db.execute('SELECT id FROM checks ORDER BY priority')]
        self.assertEqual(ids, ['tt7678620:1:1', 'tt7678620:1:2', 'tt7678620:0:1'])
        self.assertEqual(self.app.store.get_tmdb_identity('series', 82728, time.time())['imdb_id'], 'tt7678620')

    def test_tmdb_failure_keeps_stored_metadata_and_retries(self):
        self.app.tmdb_meta._fetch = Transport(TMDBMetadataTests().series_routes())
        self.app.fetch_meta('series', 'tt7678620')
        stored = self.app.store.db.execute('SELECT meta FROM titles').fetchone()['meta']
        self.app.tmdb_meta._fetch = Transport({'/tv/82728': TMDBError('TMDB request failed'),
                                               '/find/tt7678620': TMDBError('TMDB request failed')})
        self.app.fetch_meta('series', 'tt7678620')
        row = self.app.store.db.execute('SELECT meta,meta_due FROM titles').fetchone()
        self.assertEqual(row['meta'], stored)
        self.assertLess(row['meta_due'], time.time() + 1801)
        self.assertEqual(self.app.metadata_errors, 1)
        self.app.tmdb_meta._fetch = Transport({})  # Authoritative 404 everywhere.
        self.app.fetch_meta('series', 'tt7678620')
        row = self.app.store.db.execute('SELECT meta,meta_due FROM titles').fetchone()
        self.assertEqual(row['meta'], stored)
        self.assertGreater(row['meta_due'], time.time() + 80000)

    def test_identity_outside_tmdb_keeps_the_source_profile_provider(self):
        self.app.tmdb_meta._fetch = lambda path, params: self.fail('TMDB cannot address ' + path)
        seen = []
        self.app.request = lambda path, **kwargs: seen.append((path, kwargs)) or {'meta': None}
        self.app.fetch_meta('series', 'kitsu:12')
        self.assertEqual(seen, [('/meta/series/kitsu%3A12.json', {'catalog': True})])
        self.assertEqual(self.app.metadata_errors, 0)

    def test_without_a_credential_the_source_profile_is_still_used(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        options = dict(self.app.o, tmdb_api_key='')
        app = App(options, temp.name + '/db')
        self.addCleanup(app.store.db.close)
        self.assertIsNone(app.tmdb_meta)
        seen = []
        app.request = lambda path, **kwargs: seen.append((path, kwargs)) or {'meta': None}
        app.fetch_meta('movie', 'tt0816692')
        self.assertEqual(seen, [('/meta/movie/tt0816692.json', {'catalog': True})])


if __name__ == '__main__':
    unittest.main()
