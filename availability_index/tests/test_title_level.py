"""Title-level verification: two probes per series, migration from per-episode checks."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from server import PARKED_DUE, Store

PAST = '2000-01-01T00:00:00Z'


def videos(ident, seasons=3, episodes=4):
    return [{'id': '%s:%d:%d' % (ident, s, e), 'season': s, 'episode': e, 'released': PAST}
            for s in range(1, seasons + 1) for e in range(1, episodes + 1)]


class TitleLevelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/db'
        self.store = Store(self.path, positive=3600, negative=7200)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.db.close())
        self.now = time.time()
        self.store.add_categories([{'id': 'shows', 'type': 'series', 'name': 'Shows'}], 'scanner')
        cat = self.store.db.execute('SELECT * FROM categories').fetchone()
        self.store.add_page(cat, [{'id': 'tt1', 'name': 'One'}, {'id': 'tt2', 'name': 'Two'}], 0, 100)

    def checks(self, parent):
        return {r['id']: dict(r) for r in self.store.db.execute(
            "SELECT * FROM checks WHERE type='series' AND parent=?", (parent,))}

    def legacy(self, ident, available=()):
        """A pre-0.11 show: stored metadata plus a check for every aired episode."""
        listed = videos(ident)
        self.store.db.execute("UPDATE titles SET meta=? WHERE type='series' AND id=?",
                              (json.dumps({'id': ident, 'videos': listed}), ident))
        for priority, video in enumerate(listed):
            self.store.db.execute('INSERT INTO checks(type,id,parent,priority) VALUES (?,?,?,?)',
                                  ('series', video['id'], ident, priority))
        self.store.db.commit()
        for episode in available:
            self.store.record('series', episode, ident, 'available', 1, self.now)

    def test_migration_keeps_probes_and_parks_other_confirmations(self):
        self.legacy('tt1', available=['tt1:1:3'])
        self.legacy('tt2', available=['tt2:3:1'])
        self.store.db.execute("UPDATE titles SET meta='not json' WHERE id='tt2'")
        self.store.db.commit()
        result = self.store.adopt_title_level_checks(now=self.now + 1)
        self.assertEqual(result, {'shows': 1, 'series_checks_before': 24, 'series_checks_after': 15})
        rows = self.checks('tt1')
        self.assertEqual(set(rows), {'tt1:3:1', 'tt1:1:1', 'tt1:1:3'})
        self.assertEqual((rows['tt1:3:1']['priority'], rows['tt1:1:1']['priority']), (0, 1))
        # The confirmed non-probe episode keeps the show visible until it expires,
        # and is never scheduled again; nothing is copied onto the probes.
        self.assertEqual(rows['tt1:1:3']['due'], PARKED_DUE)
        self.assertEqual(rows['tt1:3:1']['status'], 'pending')
        self.assertTrue(self.store.visible('series', 'tt1', self.now + 1))
        self.assertEqual(len(self.store.meta('series', 'tt1', self.now + 1)['videos']), 12)
        # Unreadable stored metadata is left for the next metadata fetch.
        self.assertEqual(len(self.checks('tt2')), 12)
        self.assertIsNone(self.store.adopt_title_level_checks(now=self.now + 2))  # One-time only.

    def test_policy_change_drops_parked_rows_instead_of_rechecking_them(self):
        self.legacy('tt1', available=['tt1:1:3'])
        self.store.adopt_title_level_checks(now=self.now + 1)
        self.store.invalidate()
        rows = self.checks('tt1')
        self.assertEqual(set(rows), {'tt1:3:1', 'tt1:1:1'})
        self.assertTrue(all(r['status'] == 'pending' and r['due'] == 0 for r in rows.values()))

    def test_migration_survives_restart_and_is_reported(self):
        self.legacy('tt1')
        self.store.adopt_title_level_checks(now=self.now)
        self.store.db.close()
        self.store = Store(self.path, positive=3600, negative=7200)
        self.assertIsNone(self.store.adopt_title_level_checks())
        self.assertEqual(self.store.setting('title_level_checks_v1')['series_checks_after'], 2)

    def test_confirmed_probe_defers_fallback_until_after_its_renewal(self):
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': videos('tt1')}, self.now)
        self.store.record('series', 'tt1:3:1', 'tt1', 'available', 1, self.now)
        rows = self.checks('tt1')
        renewal = rows['tt1:3:1']['due']
        self.assertAlmostEqual(renewal, self.now + 3600 * .8, delta=1)
        self.assertAlmostEqual(rows['tt1:1:1']['due'], renewal + 600, delta=1)
        # If the renewal only errors, the fallback runs ten minutes later.
        self.store.record('series', 'tt1:3:1', 'tt1', 'error', 0, renewal)
        self.assertLess(self.checks('tt1')['tt1:1:1']['due'], renewal + 601)
        self.assertGreater(self.checks('tt1')['tt1:3:1']['due'], renewal + 200)

    def test_renumbered_episode_list_is_refused_against_the_listed_episodes(self):
        # After the migration a show has only two checks; the numbering guard
        # compares TMDB's list with the episodes SenPlayer currently shows.
        imdb_layout = videos('tt1', seasons=2, episodes=8)
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': imdb_layout}, self.now)
        self.store.record('series', 'tt1:2:1', 'tt1', 'available', 1, self.now)
        stored = self.store.db.execute("SELECT meta FROM titles WHERE id='tt1'").fetchone()[0]
        flat = videos('tt1', seasons=1, episodes=16)
        self.store.save_meta('series', 'tt1', {'id': 'tt1', 'videos': flat}, self.now + 5, keep_identity=True)
        self.assertEqual(self.store.db.execute("SELECT meta FROM titles WHERE id='tt1'").fetchone()[0], stored)
        self.assertIn(('series', 'tt1'), self.store.identity_conflicts)
        self.assertEqual(set(self.checks('tt1')), {'tt1:2:1', 'tt1:1:1'})


if __name__ == '__main__':
    unittest.main()
