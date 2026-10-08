"""Direct discovery preserves identity, paging, filters and safe failure semantics."""
import io
import json
import os
import sys
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tmdb_discovery import TMDBDiscovery, TMDBError, _NoRedirect


def definition(kind='movie', **filters):
    return {'id': 'original-upstream.' + kind, 'name': 'Best ' + kind, 'type': kind,
            'filters': {'listType': 'discover', 'imdbOnly': False, 'includeAdult': False,
                        'releasedOnly': True, **filters}}


def item(ident=1, kind='movie'):
    return {'id': ident, 'title' if kind == 'movie' else 'name': 'Title ' + str(ident),
            'overview': 'Description', 'genre_ids': [16, 10751],
            'release_date' if kind == 'movie' else 'first_air_date': '2020-08-14',
            'poster_path': '/poster.jpg', 'backdrop_path': '/backdrop.jpg'}


def response(page=1, total=20, kind='movie'):
    start, end = (page - 1) * 20, min(page * 20, total)
    return {'page': page, 'total_pages': (total + 19) // 20, 'total_results': total,
            'results': [item(i + 1, kind) for i in range(start, end)]}


class DiscoveryTests(unittest.TestCase):
    def adapter(self, catalogs=None, request=None, resolver=None):
        return TMDBDiscovery('a' * 32, catalogs or [definition()], request_json=request or Mock(return_value=response()),
                             today=lambda: '2026-10-08', resolve_identity=resolver or (lambda kind, ident: 'tt%07d' % ident))

    def test_manifest_keeps_exact_shelf_ids_and_definitions_are_copied(self):
        definitions = [definition(), definition('series')]
        adapter = self.adapter(definitions)
        definitions[0]['id'] = 'changed'
        public = adapter.manifest_catalogs()
        self.assertEqual([c['id'] for c in public], ['original-upstream.movie', 'original-upstream.series'])
        self.assertTrue(adapter.has_catalog('movie', 'original-upstream.movie'))
        self.assertFalse(adapter.has_catalog('series', 'original-upstream.movie'))
        self.assertNotIn('filters', public[0])
        public[0]['name'] = 'changed'
        self.assertEqual(adapter.manifest_catalogs()[0]['name'], 'Best movie')

    def test_best_movie_filters_keep_rating_votes_and_all_release_types(self):
        request = Mock(return_value=response())
        adapter = self.adapter([definition(sortBy='vote_average.desc', ratingMin=7, voteCountMin=500,
                                            releaseTypes=[1, 2, 3, 4, 5, 6])], request)
        result = adapter.catalog('movie', 'original-upstream.movie')
        path, params = request.call_args.args
        self.assertEqual(path, '/discover/movie')
        self.assertEqual(params['vote_average.gte'], 7)
        self.assertEqual(params['vote_count.gte'], 500)
        self.assertEqual(params['with_release_type'], '1|2|3|4|5|6')
        self.assertEqual(params['primary_release_date.lte'], '2026-10-08')
        self.assertEqual(params['include_adult'], 'false')
        self.assertEqual(params['include_video'], 'false')
        self.assertNotIn('with_original_language', params)
        self.assertTrue(result['complete'])
        self.assertEqual(result['next_offset'], 20)

    def test_series_filters_use_or_scripted_types_and_exclude_unscripted_genres(self):
        request = Mock(return_value=response(kind='series'))
        adapter = self.adapter([definition('series', sortBy='first_air_date.desc', tvType='2|4',
                                            genres=[16], excludeGenres=[10763, 10764, 10767], voteCountMin=50)], request)
        adapter.catalog('series', 'original-upstream.series')
        path, params = request.call_args.args
        self.assertEqual(path, '/discover/tv')
        self.assertEqual(params['with_type'], '2|4')
        self.assertEqual(params['without_genres'], '10763|10764|10767')
        self.assertEqual(params['with_genres'], '16')
        self.assertEqual(params['first_air_date.lte'], '2026-10-08')
        self.assertEqual(params['with_status'], '0|3|4|5')
        self.assertEqual(params['include_null_first_air_dates'], 'false')
        self.assertNotIn('with_release_type', params)

    def test_genre_and_mode_and_zero_ratings_are_not_lost(self):
        request = Mock(return_value=response())
        adapter = self.adapter([definition(genres=[14, 878], genreMatchMode='all', ratingMin=0,
                                            ratingMax=8, voteCountMin=0, releasedOnly=False)], request)
        adapter.catalog('movie', 'original-upstream.movie')
        params = request.call_args.args[1]
        self.assertEqual(params['with_genres'], '14,878')
        self.assertEqual(params['vote_average.gte'], 0)
        self.assertEqual(params['vote_count.gte'], 0)
        self.assertNotIn('primary_release_date.lte', params)

    def test_tmdb_discovery_does_not_replace_imdb_identity_or_mislabel_ratings(self):
        adapter = self.adapter(request=Mock(return_value=response(total=1)))
        preview = adapter.catalog('movie', 'original-upstream.movie')['metas'][0]
        self.assertEqual(preview['id'], 'tt0000001')
        self.assertEqual(preview['imdb_id'], 'tt0000001')
        self.assertEqual(preview['tmdbId'], 1)
        self.assertEqual(preview['releaseInfo'], '2020')
        self.assertEqual(preview['genres'], ['Animation', 'Family'])
        self.assertEqual(preview['poster'], 'https://image.tmdb.org/t/p/w500/poster.jpg')
        self.assertNotIn('imdbRating', preview)

    def test_missing_imdb_mapping_keeps_tmdb_identity_instead_of_dropping_title(self):
        adapter = self.adapter(request=Mock(return_value=response(total=1)), resolver=lambda *_: None)
        preview = adapter.catalog('movie', 'original-upstream.movie')['metas'][0]
        self.assertEqual(preview['id'], 'tmdb:1')
        self.assertNotIn('imdb_id', preview)

    def test_pagination_is_nonoverlapping_and_last_short_page_is_authoritative(self):
        request = Mock(side_effect=[response(1, 23), response(2, 23)])
        adapter = self.adapter(request=request)
        first = adapter.catalog('movie', 'original-upstream.movie', 0)
        last = adapter.catalog('movie', 'original-upstream.movie', first['next_offset'])
        self.assertFalse(first['complete'])
        self.assertTrue(last['complete'])
        self.assertEqual(len(first['metas']), 20)
        self.assertEqual(len(last['metas']), 3)
        self.assertEqual(last['next_offset'], 23)
        self.assertFalse({m['id'] for m in first['metas']} & {m['id'] for m in last['metas']})
        self.assertEqual(request.call_args.args[1]['page'], 2)

    def test_unaligned_offset_is_sliced_without_repeating_or_skipping_next_page(self):
        request = Mock(side_effect=[response(1, 40), response(2, 40)])
        adapter = self.adapter(request=request)
        partial = adapter.catalog('movie', 'original-upstream.movie', 7)
        second = adapter.catalog('movie', 'original-upstream.movie', partial['next_offset'])
        self.assertEqual([m['tmdbId'] for m in partial['metas']], list(range(8, 21)))
        self.assertEqual(second['metas'][0]['tmdbId'], 21)

    def test_authoritative_empty_is_distinct_from_a_provider_failure(self):
        adapter = self.adapter(request=Mock(return_value=response(total=0)))
        self.assertEqual(adapter.catalog('movie', 'original-upstream.movie'),
                         {'metas': [], 'complete': True, 'next_offset': 0})
        for bad in ({}, {'success': False, 'status_message': 'secret'}, {'results': []},
                    {'page': 1, 'total_pages': 2, 'total_results': 21, 'results': []}):
            with self.subTest(bad=bad):
                adapter = self.adapter(request=Mock(return_value=bad))
                with self.assertRaises(TMDBError):
                    adapter.catalog('movie', 'original-upstream.movie')

    def test_missing_or_malformed_items_fail_whole_page_instead_of_truncating_cursor(self):
        malformed = response(1, 40)
        malformed['results'][3].pop('id')
        with self.assertRaises(TMDBError):
            self.adapter(request=Mock(return_value=malformed)).catalog('movie', 'original-upstream.movie')
        partial = response(1, 40)
        partial['results'].pop()
        with self.assertRaises(TMDBError):
            self.adapter(request=Mock(return_value=partial)).catalog('movie', 'original-upstream.movie')

    def test_contradictory_totals_or_short_final_pages_cannot_authorize_pruning(self):
        cases = [
            (1, 1, 100, 1),  # Falsely claims the first short page is the last.
            (1, 1, 100, 20),  # Even a full page cannot explain the totals.
            (1, 2, 20, 20),  # Extra claimed page with no remaining results.
            (2, 2, 23, 2),  # Final page is missing one title.
            (2, 2, 23, 4),  # Final page has an unexplained extra title.
            (1, 0, 1, 1),
            (1, 2, 0, 0),
        ]
        for page, total_pages, total_results, count in cases:
            payload = {'page': page, 'total_pages': total_pages, 'total_results': total_results,
                       'results': [item(i + 1) for i in range(count)]}
            with self.subTest(case=(page, total_pages, total_results, count)), self.assertRaises(TMDBError):
                self.adapter(request=Mock(return_value=payload)).catalog(
                    'movie', 'original-upstream.movie', (page - 1) * 20)

    def test_empty_one_page_representation_is_authoritative(self):
        payload = response(total=0)
        payload['total_pages'] = 1
        self.assertEqual(self.adapter(request=Mock(return_value=payload)).catalog('movie', 'original-upstream.movie'),
                         {'metas': [], 'complete': True, 'next_offset': 0})

    def test_page_count_may_be_capped_at_500_but_items_still_must_match(self):
        payload = response(500, 12000)
        payload['total_pages'] = 500
        result = self.adapter(request=Mock(return_value=payload)).catalog('movie', 'original-upstream.movie', 9980)
        self.assertTrue(result['complete'])
        self.assertEqual(result['next_offset'], 10000)
        payload['results'].pop()
        with self.assertRaises(TMDBError):
            self.adapter(request=Mock(return_value=payload)).catalog('movie', 'original-upstream.movie', 9980)

    def test_identity_lookup_failure_does_not_publish_fallback_or_empty_page(self):
        def failed(*_):
            raise TMDBError('TMDB request failed')
        with self.assertRaises(TMDBError):
            self.adapter(resolver=failed).catalog('movie', 'original-upstream.movie')

    def test_page_limit_stops_cleanly_without_requesting_invalid_page_501(self):
        request = Mock(return_value=response(500, 12000))
        result = self.adapter(request=request).catalog('movie', 'original-upstream.movie', 9980)
        self.assertTrue(result['complete'])
        self.assertEqual(result['next_offset'], 10000)
        with self.assertRaises(ValueError):
            self.adapter(request=request).catalog('movie', 'original-upstream.movie', 10000)
        self.assertEqual(request.call_count, 1)

    def test_unknown_filters_types_or_imdb_only_are_not_silently_ignored(self):
        for extra in ({'watchProviders': [1]}, {'imdbOnly': True}, {'tvType': '4'},
                      {'genres': [True]}, {'ratingMin': float('nan')}, {'ratingMin': 9, 'ratingMax': 8},
                      {'releasedOnly': 'true'}, {'releaseTypes': [7]}, {'voteCountMin': True},
                      {'sortBy': 'first_air_date.desc'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.adapter([definition(**extra)])


class IdentityAndTransportTests(unittest.TestCase):
    def test_external_identity_cache_keeps_types_separate_and_caches_confirmed_null(self):
        request = Mock(side_effect=[{'id': 1, 'imdb_id': 'tt0000001'}, {'id': 1, 'imdb_id': None}])
        adapter = TMDBDiscovery('a' * 32, [definition()], request_json=request)
        self.assertEqual(adapter._identity('movie', 1), 'tt0000001')
        self.assertEqual(adapter._identity('movie', 1), 'tt0000001')
        self.assertIsNone(adapter._identity('series', 1))
        self.assertIsNone(adapter._identity('series', 1))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[0], '/tv/1/external_ids')

    def test_failed_external_lookup_is_not_cached_as_missing_imdb(self):
        request = Mock(side_effect=[URLError('api_key=secret'), {'id': 1, 'imdb_id': 'tt0000001'}])
        adapter = TMDBDiscovery('a' * 32, [definition()], request_json=request)
        with self.assertRaises(TMDBError) as caught:
            adapter._identity('movie', 1)
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(adapter._identity('movie', 1), 'tt0000001')
        self.assertEqual(request.call_count, 2)

    def test_external_lookup_requires_explicit_response_for_exact_title(self):
        for data in ({'id': 1}, {'id': 2, 'imdb_id': 'tt0000001'}, {'id': 1, 'imdb_id': '1'},
                     {'id': 1, 'imdb_id': ['tt0000001']}):
            with self.subTest(data=data), self.assertRaises(TMDBError):
                TMDBDiscovery('a' * 32, [definition()], request_json=Mock(return_value=data)).external_id('movie', 1)

    def test_v3_api_key_is_only_sent_to_official_https_api_and_not_public_manifest(self):
        adapter = TMDBDiscovery('a' * 32, [definition()])
        adapter._opener = Mock()
        adapter._opener.open.return_value = io.StringIO('{}')
        adapter._http_json('/discover/movie', {'page': 1})
        request = adapter._opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc), ('https', 'api.themoviedb.org'))
        self.assertEqual(parse_qs(url.query)['api_key'], ['a' * 32])
        self.assertNotIn('a' * 32, json.dumps(adapter.manifest_catalogs()))

    def test_read_access_token_is_header_only(self):
        adapter = TMDBDiscovery('read-access-token', [definition()])
        adapter._opener = Mock()
        adapter._opener.open.return_value = io.StringIO('{}')
        adapter._http_json('/discover/movie', {'page': 1})
        request = adapter._opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer read-access-token')
        self.assertNotIn('read-access-token', request.full_url)
        self.assertNotIn('api_key', request.full_url)

    def test_http_and_json_errors_never_leak_secret_url_or_response_body(self):
        for failure in (HTTPError('https://api.themoviedb.org?api_key=secret', 429, 'secret', {}, None),
                        URLError('secret'), ValueError('secret')):
            with self.subTest(error=type(failure).__name__):
                adapter = TMDBDiscovery('a' * 32, [definition()])
                adapter._opener = Mock()
                adapter._opener.open.side_effect = failure
                with self.assertRaises(TMDBError) as caught:
                    adapter._http_json('/discover/movie', {})
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn('a' * 32, str(caught.exception))

    def test_redirects_are_refused(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.invalid'))


if __name__ == '__main__':
    unittest.main()
