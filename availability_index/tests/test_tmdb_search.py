"""Title discovery is bounded, multilingual and never an availability proof."""
import copy
import os
import sys
import time
import unittest
from unittest.mock import Mock
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tmdb_discovery import TMDBDiscovery, TMDBError


def item(ident=1, kind='movie', **fields):
    return {'id': ident, 'title' if kind == 'movie' else 'name': 'Puss in Boots',
            'original_title' if kind == 'movie' else 'original_name': 'Puss in Boots',
            'release_date' if kind == 'movie' else 'first_air_date': '2011-10-27',
            'genre_ids': [16, 10751], **fields}


def page(items=(), total=None):
    total = len(items) if total is None else total
    return {'page': 1, 'total_pages': (total + 19) // 20,
            'total_results': total, 'results': list(items)}


class SearchTests(unittest.TestCase):
    def adapter(self, request, resolver=None, language='en-US'):
        return TMDBDiscovery('a' * 32, [{'id': 'movies', 'name': 'Movies', 'type': 'movie'}],
                             language=language, request_json=request, today=lambda: '2026-10-11',
                             resolve_identity=resolver or (lambda kind, ident: 'tt%07d' % ident))

    def test_movie_search_uses_official_search_parameters_and_only_first_page(self):
        request = Mock(return_value=page([item(i) for i in range(1, 21)], total=135))
        metas = self.adapter(request).search('movie', '  Puss   in Boots  ')['metas']
        self.assertEqual(len(metas), 20)
        request.assert_called_once_with('/search/movie', {
            'page': 1, 'language': 'en-US', 'include_adult': 'false', 'query': 'Puss in Boots'})
        self.assertTrue(all(m['id'].startswith('tt') for m in metas))
        self.assertTrue(all('streams' not in m and 'videos' not in m for m in metas))

    def test_articles_are_retried_only_after_confirmed_empty_search(self):
        request = Mock(side_effect=[page(), page([item()])])
        metas = self.adapter(request).search('movie', 'puss in the boots')['metas']
        self.assertEqual(len(metas), 1)
        self.assertEqual([call.args[1]['query'] for call in request.call_args_list],
                         ['puss in the boots', 'puss in boots'])

    def test_existing_hits_are_not_broadened_by_article_removal(self):
        request = Mock(return_value=page([item(title='The Thing')]))
        self.adapter(request).search('movie', 'The Thing')
        self.assertEqual(request.call_count, 1)

    def test_articles_alone_or_titles_without_articles_do_not_retry(self):
        for query in ('the', 'an a the', 'Bluey', 'Theatre'):
            with self.subTest(query=query):
                request = Mock(return_value=page())
                self.assertEqual(self.adapter(request).search('movie', query), {'metas': []})
                self.assertEqual(request.call_count, 1)

    def test_russian_query_gets_translated_name_and_retains_original_name(self):
        request = Mock(return_value=page([item(title='Кот в сапогах')]))
        preview = self.adapter(request).search('movie', 'Кот в сапогах')['metas'][0]
        self.assertEqual(request.call_args.args[1]['language'], 'ru-RU')
        self.assertEqual(preview['name'], 'Кот в сапогах')
        self.assertEqual(preview['original_title'], 'Puss in Boots')

    def test_series_search_uses_tv_dates_and_preserves_original_name(self):
        request = Mock(return_value=page([item(kind='series', name='Приключения Кота в сапогах',
                                                  original_name='The Adventures of Puss in Boots')]))
        preview = self.adapter(request).search('series', 'Приключения Кота в сапогах')['metas'][0]
        self.assertEqual(request.call_args.args[0], '/search/tv')
        self.assertEqual(preview['type'], 'series')
        self.assertEqual(preview['original_name'], 'The Adventures of Puss in Boots')
        self.assertEqual(preview['releaseInfo'], '2011')

    def test_non_cyrillic_query_keeps_configured_metadata_language(self):
        request = Mock(return_value=page([item()]))
        self.adapter(request, language='de-DE').search('movie', 'Der gestiefelte Kater')
        self.assertEqual(request.call_args.args[1]['language'], 'de-DE')

    def test_series_search_excludes_news_reality_and_talk_before_identity_lookup(self):
        rows = [item(1, kind='series', genre_ids=[10763]),
                item(2, kind='series', genre_ids=[10764, 35]),
                item(3, kind='series', genre_ids=[10767]),
                item(4, kind='series', genre_ids=[16, 10762])]
        resolver = Mock(return_value='tt0000004')
        result = self.adapter(Mock(return_value=page(rows)), resolver).search('series', 'Puss')
        self.assertEqual([m['id'] for m in result['metas']], ['tt0000004'])
        resolver.assert_called_once_with('series', 4)

    def test_series_search_rejects_malformed_genres_instead_of_skipping_validation(self):
        for genres in (None, '10763', [True], [10763, 'bad']):
            with self.subTest(genres=genres), self.assertRaises(TMDBError):
                self.adapter(Mock(return_value=page([item(kind='series', genre_ids=genres)]))).search('series', 'Puss')

    def test_adult_future_invalid_and_unknown_dates_are_excluded_before_identity_lookup(self):
        rows = [item(1, adult=True), item(2, release_date='2026-10-12'),
                item(3, release_date=''), item(4, release_date=None),
                item(5, release_date='2026-02-30'), item(6, release_date='2026-10-11')]
        resolver = Mock(return_value='tt0000006')
        request = Mock(return_value=page(rows))
        metas = self.adapter(request, resolver).search('movie', 'The example')['metas']
        self.assertEqual([m['id'] for m in metas], ['tt0000006'])
        resolver.assert_called_once_with('movie', 6)
        self.assertEqual(request.call_count, 1)

    def test_all_filtered_results_do_not_trigger_broader_query(self):
        request = Mock(return_value=page([item(adult=True)]))
        self.assertEqual(self.adapter(request).search('movie', 'The example'), {'metas': []})
        self.assertEqual(request.call_count, 1)

    def test_bad_pagination_and_malformed_items_fail_instead_of_claiming_no_match(self):
        malformed = item()
        malformed.pop('id')
        bad_pages = [{}, {'results': []}, page([malformed]),
                     dict(page(), total_results=21, total_pages=2),
                     dict(page([item()]), page=2), dict(page([item()]), total_pages=True),
                     page([item(i) for i in range(21)]), page([None])]
        for payload in bad_pages:
            with self.subTest(payload=payload), self.assertRaises(TMDBError):
                self.adapter(Mock(return_value=payload)).search('movie', 'The example')

    def test_provider_failure_is_safe_and_not_retried_as_an_empty_query(self):
        request = Mock(side_effect=URLError('api_key=secret'))
        with self.assertRaises(TMDBError) as caught:
            self.adapter(request).search('movie', 'The example')
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(request.call_count, 1)

    def test_identity_failure_propagates_without_inventing_tmdb_fallback(self):
        resolver = Mock(side_effect=TMDBError('TMDB request failed'))
        with self.assertRaises(TMDBError):
            self.adapter(Mock(return_value=page([item()])), resolver).search('movie', 'Puss in Boots')

    def test_confirmed_absent_imdb_id_can_keep_exact_tmdb_identity(self):
        resolver = Mock(return_value=None)
        preview = self.adapter(Mock(return_value=page([item()])), resolver).search('movie', 'Puss')['metas'][0]
        self.assertEqual(preview['id'], 'tmdb:1')

    def test_deadline_stops_before_network_and_between_identity_expansions(self):
        request = Mock(return_value=page([item(1), item(2)]))
        with self.assertRaises(TMDBError):
            self.adapter(request).search('movie', 'Puss', deadline=time.monotonic() - 1)
        request.assert_not_called()
        # The injected clock gives the first request/title a budget, then stops
        # before resolving a second title instead of allowing 20 slow lookups.
        from unittest.mock import patch
        resolver = Mock(return_value='tt0000001')
        with patch('tmdb_discovery.time.monotonic', side_effect=[0, 1, 3]):
            with self.assertRaises(TMDBError):
                self.adapter(request, resolver).search('movie', 'Puss', deadline=2)
        resolver.assert_called_once_with('movie', 1)

    def test_invalid_query_or_type_does_not_contact_tmdb(self):
        request = Mock()
        adapter = self.adapter(request)
        for kind, query in [('episode', 'Puss'), ('movie', None), ('movie', ''),
                            ('movie', '   '), ('movie', 'x' * 201)]:
            with self.subTest(kind=kind, query=query), self.assertRaises(ValueError):
                adapter.search(kind, query)
        request.assert_not_called()

    def test_catalog_previews_also_preserve_original_title_without_mutating_response(self):
        source = page([item(title='Puss in Boots', original_title='Кот в сапогах')])
        before = copy.deepcopy(source)
        result = self.adapter(Mock(return_value=source)).catalog('movie', 'movies')
        self.assertEqual(result['metas'][0]['original_title'], 'Кот в сапогах')
        self.assertEqual(source, before)


if __name__ == '__main__':
    unittest.main()
