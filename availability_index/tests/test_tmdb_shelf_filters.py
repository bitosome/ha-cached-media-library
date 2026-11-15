"""Shelf selection filters: languages, origin, certification, keywords, age."""
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tmdb_discovery import TMDBDiscovery, TMDBError


def definition(kind='movie', **filters):
    return {'id': 'shelf.' + kind, 'name': 'Shelf', 'type': kind,
            'filters': {'listType': 'discover', 'releasedOnly': True, **filters}}


def page(languages, kind='movie'):
    items = [{'id': n + 1, 'title' if kind == 'movie' else 'name': 'Title ' + str(n + 1), 'genre_ids': [16],
              'release_date' if kind == 'movie' else 'first_air_date': '2020-08-14',
              'original_language': language} for n, language in enumerate(languages)]
    return {'page': 1, 'total_pages': 1, 'total_results': len(items), 'results': items}


class ShelfFilterTests(unittest.TestCase):
    def adapter(self, catalog, request):
        return TMDBDiscovery('a' * 32, [catalog], request_json=request, today=lambda: '2026-11-15',
                             resolve_identity=lambda kind, ident: 'tt%07d' % ident)

    def params(self, catalog):
        request = Mock(return_value=page(['en'], catalog['type']))
        self.adapter(catalog, request).catalog(catalog['type'], catalog['id'])
        return request.call_args.args[1]

    def test_selection_parameters_are_sent_to_tmdb(self):
        params = self.params(definition(originalLanguages=['en', 'ru'], originCountries=['US', 'GB'],
                                        certificationCountry='US', certificationMax='PG',
                                        keywords=[1, 2], excludeKeywords=[210024, 5], companies=[3]))
        self.assertEqual(params['with_original_language'], 'en|ru')
        self.assertEqual(params['with_origin_country'], 'US|GB')
        self.assertEqual((params['certification_country'], params['certification.lte']), ('US', 'PG'))
        self.assertEqual((params['with_keywords'], params['without_keywords']), ('1|2', '210024,5'))
        self.assertEqual(params['with_companies'], '3')
        self.assertEqual(self.params(definition('series', networks=[213, 49]))['with_networks'], '213|49')

    def test_minimum_age_moves_the_upper_release_bound(self):
        self.assertEqual(self.params(definition(releasedBeforeDays=365))['primary_release_date.lte'], '2025-11-15')
        params = self.params(definition('series', releasedBeforeDays=30, releasedWithinDays=365))
        self.assertEqual((params['first_air_date.gte'], params['first_air_date.lte']), ('2025-11-15', '2026-10-16'))

    def test_languages_are_enforced_locally_without_moving_the_cursor(self):
        request = Mock(return_value=page(['en', 'ja', 'ko', 'fr']))
        result = self.adapter(definition(excludeOriginalLanguages=['ja', 'ko']), request).catalog('movie', 'shelf.movie')
        self.assertEqual([m['id'] for m in result['metas']], ['tt0000001', 'tt0000004'])
        self.assertEqual((result['next_offset'], result['complete']), (4, True))
        self.assertNotIn('with_original_language', request.call_args.args[1])
        request = Mock(return_value=page(['en', 'ja', 'ru']))
        result = self.adapter(definition(originalLanguages=['en', 'ru']), request).catalog('movie', 'shelf.movie')
        self.assertEqual([m['id'] for m in result['metas']], ['tt0000001', 'tt0000003'])

    def test_now_playing_accepts_language_selection_only(self):
        cinema = {'id': 'cinema', 'type': 'movie', 'name': 'In Cinemas',
                  'filters': {'listType': 'now_playing', 'region': 'EE', 'excludeOriginalLanguages': ['hi']}}
        request = Mock(return_value=page(['en', 'hi']))
        result = self.adapter(cinema, request).catalog('movie', 'cinema')
        self.assertEqual([m['id'] for m in result['metas']], ['tt0000001'])
        self.assertEqual(set(request.call_args.args[1]), {'page', 'language', 'region'})
        cinema['filters']['excludeKeywords'] = [1]
        with self.assertRaises(ValueError):
            self.adapter(cinema, request)

    def test_item_without_a_language_fails_the_page_when_a_language_rule_applies(self):
        broken = page(['en'])
        del broken['results'][0]['original_language']
        with self.assertRaises(TMDBError):
            self.adapter(definition(excludeOriginalLanguages=['ja']), Mock(return_value=broken)).catalog('movie', 'shelf.movie')
        self.assertEqual(len(self.adapter(definition(), Mock(return_value=broken)).catalog('movie', 'shelf.movie')['metas']), 1)

    def test_retired_shelf_is_claimed_but_never_published_or_crawled(self):
        gone = {'id': 'old.shelf', 'type': 'movie', 'name': 'Fantasy Movies', 'retired': True}
        adapter = TMDBDiscovery('a' * 32, [definition(), gone], request_json=Mock())
        self.assertEqual([c['id'] for c in adapter.manifest_catalogs()], ['shelf.movie'])
        self.assertEqual(adapter.claimed(), {('movie', 'shelf.movie'), ('movie', 'old.shelf')})
        self.assertFalse(adapter.has_catalog('movie', 'old.shelf'))
        with self.assertRaises(ValueError):
            adapter.catalog('movie', 'old.shelf')
        for bad in ([gone], [definition(), gone, dict(gone)], [definition(), dict(gone, retired='yes')],
                    [dict(definition(), surprise=1)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                TMDBDiscovery('a' * 32, bad, request_json=Mock())


    def test_invalid_selection_filters_fail_configuration(self):
        bad = [dict(originalLanguages=['EN']), dict(originalLanguages=[]), dict(excludeOriginalLanguages='ja'),
               dict(originalLanguages=['en'], excludeOriginalLanguages=['en']), dict(originalLanguages=['en', 'en']),
               dict(originCountries=['us']), dict(certificationCountry='US'), dict(certificationMax='PG'),
               dict(certificationCountry='US', certificationMax='PG 13'), dict(keywords=[]), dict(keywords=['1']),
               dict(excludeKeywords=[0]), dict(companies=list(range(1, 22))), dict(networks=[213]),
               dict(releasedBeforeDays=0), dict(releasedBeforeDays=True), dict(releasedBeforeDays=10, releasedOnly=False),
               dict(releasedBeforeDays=200, releasedWithinDays=180)]
        for filters in bad:
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.adapter(definition(**filters), Mock())
        with self.assertRaises(ValueError):
            self.adapter(definition('series', certificationCountry='US', certificationMax='TV-Y'), Mock())


if __name__ == '__main__':
    unittest.main()
