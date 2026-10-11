import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from search_matching import normalized_title, search_score


class SearchMatchingTests(unittest.TestCase):
    def test_articles_punctuation_spacing_accents_and_word_order(self):
        for query, name in [('puss in the boots', 'Puss in Boots'),
                            ('puss in boots the last wish', 'Puss in Boots: The Last Wish'),
                            ('  Puss  in Boots ', 'Puss in Boots'), ('boots puss', 'Puss in Boots'),
                            ('wall-e', 'WALL·E'), ('amelie', 'Amélie'), ('ежик', 'Ёжик')]:
            with self.subTest(query=query):
                self.assertIsNotNone(search_score(query, name))

    def test_exact_title_wins_over_sequels_and_word_matches(self):
        names = ['Puss in Boots: The Last Wish', 'The Adventures of Puss in Boots', 'Puss in Boots']
        self.assertEqual(sorted(names, key=lambda name: search_score('puss in boots', name))[0], 'Puss in Boots')
        self.assertLess(search_score('alien', 'Alien'), search_score('alien', 'Aliens'))

    def test_short_titles_and_numbers_survive_normalization(self):
        for name in ('It', 'Up', 'A', 'The', '9'):
            self.assertEqual(search_score(name, name)[0], 0)
        self.assertIsNone(search_score('it', 'Spirit'))
        self.assertIsNone(search_score('up', 'Superman'))
        self.assertIsNone(search_score('Toy Story 2', 'Toy Story 3'))
        self.assertIsNone(search_score('Toy Story 2', 'Toy Story 20'))
        self.assertIsNone(search_score('Story Toy 2', 'Toy Story 20'))
        self.assertIsNone(search_score('2', '20'))
        self.assertEqual(normalized_title('Большой'), 'большой')
        for query in ('', '  ', '...'):
            self.assertIsNone(search_score(query, 'Anything'))

    def test_query_words_cannot_be_spread_across_unrelated_aliases(self):
        self.assertIsNone(search_score('cat bear', 'The Cat\nThe Bear'))
        self.assertIsNotNone(search_score('кот в сапогах', 'Puss in Boots\nКот в сапогах'))
