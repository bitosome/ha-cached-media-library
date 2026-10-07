"""Metadata-only Stremio availability index for AIOStreams.

Publishes catalogues and search results containing only titles whose stream was
recently verified as playable under the family profile's own filters. It reads
AIOStreams through the public Stremio endpoint only: it never changes AIOStreams
configuration, never stores stream URLs, and never proxies video.
"""
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

# Include new/unknown settings conservatively. An allow-list of a few filters
# missed language, resolution, size and provider changes and kept stale verdicts.
# Only known presentation, identity and catalogue-only settings are excluded.
NON_POLICY_KEYS = frozenset({
    'uuid', 'encryptedPassword', 'accessKey', 'ip', 'showChanges',
    'manifestNotice', 'linkedAccounts', 'appliedTemplates',
    'addonName', 'addonLogo', 'addonBackground', 'addonDescription',
    'addonCategoryColors', 'formatter', 'sortCriteria', 'statistics',
    'hideErrors', 'hideErrorsForResources', 'catalogModifications',
    'newCatalogsDisabled', 'upstreamCatalogOrder', 'mergedCatalogs',
    'jellyfin', 'rpdbApiKey', 'topPosterApiKey', 'aioratingsApiKey',
    'aioratingsProfileId', 'openposterdbApiKey', 'openposterdbUrl',
    'openposterdbParameters', 'posterService', 'usePosterRedirectApi',
    'usePosterServiceForMeta',
})
STRICT_FILTER = "cached(service(streams, 'torbox'))"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def folded(value):
    return unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')


def playable(stream):
    # AIOStreams can give information videos HTTP URLs, so a URL alone is not
    # enough. Its structured streamData identifies these independently of labels.
    if not isinstance(stream, dict) or stream.get('error'):
        return False
    data = stream.get('streamData') or {}
    if not isinstance(data, dict) or data.get('error'):
        return False
    if (data.get('type') or stream.get('type')) in ('info', 'info-basic', 'error', 'statistic', 'external', 'p2p', 'youtube'):
        return False
    if any(str(value).startswith(('aiostreamserror.', 'error.'))
           for value in (stream.get('id', ''), data.get('id', ''))):
        return False
    service = data.get('service')
    if isinstance(service, dict) and service.get('cached') is False:
        return False
    url = stream.get('url')
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ('http', 'https') and bool(parsed.netloc)
                and 'aiostreamserror.' not in unquote(parsed.path))
    except ValueError:
        return False


def stream_verdict(result):
    if not isinstance(result, dict):
        return 'error', 0
    streams = result.get('streams')
    if not isinstance(streams, list):
        return 'error', 0
    count = sum(playable(s) for s in streams)
    if count:
        return 'available', count
    cards = [s for s in streams if isinstance(s, dict)]
    notices = ' '.join(str(s.get('description', '')) + ' ' + str(s.get('name', '')) for s in cards)
    structured_error = any(s.get('error') or (isinstance(s.get('streamData'), dict)
                           and (s['streamData'].get('error') or s['streamData'].get('type') == 'error'))
                           for s in cards)
    if (result.get('error') or result.get('errors') or structured_error
            or re.search(r'timeout|timed out|rate.?limit|unavailable|failed|error|429', notices, re.I)):
        return 'error', 0
    # hideErrors in the family profile can turn provider failures into streams:[].
    # Only an explicit filtering report proves absence; ambiguous empty responses
    # must retry without replacing or extending an earlier positive confirmation.
    filtered = any('removal reasons' in str(s.get('name', '')).casefold()
                   and re.search(r'\b(?:excluded|required|included|filtered)\b[^\n]*\([1-9]\d*\)',
                                 str(s.get('description', '')), re.I)
                   for s in cards)
    return ('unavailable' if filtered else 'error'), 0


def aired(video, now):
    if not video.get('id') or not isinstance(video.get('season'), int) or not isinstance(video.get('episode'), int):
        return False
    if video['season'] < 0 or video['episode'] < 1:
        return False
    released = video.get('released')
    if not released:  # Unknown air dates must not expose future episodes.
        return False
    try:
        return datetime.fromisoformat(released.replace('Z', '+00:00')).replace(tzinfo=timezone.utc).timestamp() <= now
    except (ValueError, TypeError):
        return False


def episode_order(video):
    """Probe the newest seasons first, and each season's opener before its filler."""
    return (0 if video['episode'] == 1 else 1, -video['season'], video['episode'])


def policy_fingerprint(config):
    policy = {k: copy.deepcopy(v) for k, v in config.items() if k not in NON_POLICY_KEYS}
    presets = []
    for preset in policy.pop('presets', []) or []:
        if not isinstance(preset, dict):
            presets.append(preset)  # Malformed/unknown settings still change policy.
            continue
        if preset.get('enabled') is False:
            continue
        options = preset.get('options') or {}
        resources = options.get('resources') if isinstance(options, dict) else None
        if isinstance(resources, list) and resources:
            names = [r.get('name') if isinstance(r, dict) else r for r in resources]
            if all(name in ('catalog', 'meta', 'subtitles', 'addon_catalog', 'watch_state') for name in names):
                continue
        item = {k: v for k, v in preset.items() if k != 'category'}
        if isinstance(resources, list) and 'stream' in [r.get('name') if isinstance(r, dict) else r for r in resources]:
            # Toggling catalogue or metadata output on a stream provider doesn't
            # change the stream policy, but retain stream-specific resource data.
            item['options']['resources'] = [r for r in resources if (r.get('name') if isinstance(r, dict) else r) == 'stream']
        presets.append(item)
    if presets:
        policy['presets'] = presets
    for key in ('variants', 'healthChecks'):
        if isinstance(policy.get(key), list):
            policy[key] = [{k: v for k, v in item.items() if k != 'name'} if isinstance(item, dict) else item
                           for item in policy[key]]
    parent = policy.get('parentConfig')
    if isinstance(parent, dict) and isinstance(parent.get('mergeStrategies'), dict):
        strategies = parent['mergeStrategies']
        for key in ('sorting', 'formatter', 'branding'):
            strategies.pop(key, None)
        if isinstance(strategies.get('fieldOverrides'), dict):
            strategies['fieldOverrides'] = {k: v for k, v in strategies['fieldOverrides'].items() if k not in NON_POLICY_KEYS}
    return hashlib.sha256(encode(policy).encode()).hexdigest()


def is_scanner_catalog(catalog_id, instance_id):
    """True for catalogues this add-on publishes, so we never index ourselves."""
    if catalog_id.startswith(instance_id):
        return True
    suffix = catalog_id.split('.', 1)[1] if '.' in catalog_id else catalog_id
    return suffix.startswith('cached-')


class Store:
    def __init__(self, path, positive=43200, negative=86400, max_episodes=12):
        self.path, self.positive, self.negative, self.max_episodes = path, positive, negative, max_episodes
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY,type TEXT,name TEXT,upstream TEXT,position INTEGER,extra TEXT,active INTEGER DEFAULT 1,offset INTEGER DEFAULT 0,done INTEGER DEFAULT 0,refresh REAL DEFAULT 0,generation REAL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS titles (type TEXT,id TEXT,preview TEXT,meta TEXT,search TEXT,meta_due REAL DEFAULT 0,meta_checked REAL DEFAULT 0,priority INTEGER DEFAULT 9999,rank INTEGER DEFAULT 999999,meta_claimed REAL DEFAULT 0,PRIMARY KEY(type,id));
          CREATE TABLE IF NOT EXISTS membership (category TEXT,type TEXT,id TEXT,rank INTEGER,seen REAL DEFAULT 0,PRIMARY KEY(category,type,id));
          CREATE TABLE IF NOT EXISTS checks (type TEXT,id TEXT,parent TEXT,status TEXT DEFAULT 'pending',checked REAL DEFAULT 0,expires REAL DEFAULT 0,due REAL DEFAULT 0,attempted REAL DEFAULT 0,failures INTEGER DEFAULT 0,count INTEGER DEFAULT 0,priority INTEGER DEFAULT 0,PRIMARY KEY(type,id,parent));
          CREATE INDEX IF NOT EXISTS due_checks ON checks(due,priority);
          CREATE INDEX IF NOT EXISTS parent_checks ON checks(type,parent,status,expires);
          CREATE INDEX IF NOT EXISTS title_memberships ON membership(type,id);
        ''')
        self.migrate()
        self.db.commit()

    def migrate(self):
        """Add columns introduced after a database was first created."""
        wanted = {'titles': [('priority', 'INTEGER DEFAULT 9999'), ('rank', 'INTEGER DEFAULT 999999'),
                             ('meta_claimed', 'REAL DEFAULT 0'), ('scan_touched', 'REAL DEFAULT 0')],
                  'categories': [('active', 'INTEGER DEFAULT 1')]}
        added = set()
        for table, columns in wanted.items():
            existing = {row[1] for row in self.db.execute('PRAGMA table_info(%s)' % table)}
            for name, ddl in columns:
                if name not in existing:
                    self.db.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, ddl))
                    added.add(name)
        if 'rank' in added:
            # Ranks are recorded while crawling, so re-crawl once to populate them.
            # Verified results survive: checks are only inserted, never reset here.
            self.db.execute('UPDATE categories SET done=0,refresh=0,offset=0')
        columns = {row[1]: row[5] for row in self.db.execute('PRAGMA table_info(checks)')}
        if 'parent' in columns and [k for k, v in columns.items() if v] == ['type', 'id']:
            # A title can be indexed under two identifiers (for example tmdb: and tt)
            # that resolve to the same episodes. Keyed by (type,id) only, the second
            # title's checks were silently discarded, so it could never be verified.
            self.db.executescript('''
              DROP INDEX IF EXISTS due_checks;
              DROP INDEX IF EXISTS parent_checks;
              ALTER TABLE checks RENAME TO checks_old;
              CREATE TABLE checks (type TEXT,id TEXT,parent TEXT,status TEXT DEFAULT 'pending',checked REAL DEFAULT 0,expires REAL DEFAULT 0,due REAL DEFAULT 0,attempted REAL DEFAULT 0,failures INTEGER DEFAULT 0,count INTEGER DEFAULT 0,priority INTEGER DEFAULT 0,PRIMARY KEY(type,id,parent));
              INSERT OR IGNORE INTO checks(type,id,parent,status,checked,expires,due,attempted,failures,count,priority)
                SELECT type,id,parent,status,checked,expires,due,attempted,failures,count,priority FROM checks_old;
              DROP TABLE checks_old;
              CREATE INDEX IF NOT EXISTS due_checks ON checks(due,priority);
              CREATE INDEX IF NOT EXISTS parent_checks ON checks(type,parent,status,expires);
            ''')
            # Re-queue so episodes whose checks were discarded are picked up again.
            self.db.execute("UPDATE titles SET meta_due=0,meta_checked=0 WHERE type='series'")
        # Repair series that were recorded as fetched but produced no episode checks,
        # which older versions could cache for a day (see the empty-list guard).
        self.db.execute('''UPDATE titles SET meta_due=0,meta_checked=0 WHERE type='series' AND meta_checked>0
          AND NOT EXISTS (SELECT 1 FROM checks c WHERE c.type='series' AND c.parent=titles.id)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS checks_parent_due ON checks(type,parent,due,priority)')
        self.db.execute('CREATE INDEX IF NOT EXISTS checks_due_status ON checks(status,due)')
        version = self.db.execute("SELECT value FROM settings WHERE key='store_schema_version'").fetchone()
        if version is None or int(json.loads(version[0])) < 2:
            # Earlier releases fetched the published (filtered) episode list and
            # permanently truncated candidate checks. Fetch original metadata once
            # after upgrading, retaining all availability confirmations meanwhile.
            self.db.execute("UPDATE titles SET meta_due=0,meta_checked=0,meta_claimed=0 WHERE type='series'")
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('store_schema_version','2')")

    @staticmethod
    def search_names(*documents):
        """Keep translated/original names searchable when previews are refreshed."""
        names = set()
        fields = ('name', 'title', 'originalName', 'originalTitle', 'original_name',
                  'original_title', 'alternativeTitles', 'alternative_titles', 'aliases', 'titles')

        def collect(value):
            if isinstance(value, str) and value.strip():
                names.add(folded(value.strip()))
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for key in fields:
                    if key in value:
                        collect(value[key])

        for document in documents:
            collect(document)
        return '\n'.join(sorted(names))

    def setting(self, key, value=None):
        with self.lock:
            if value is not None:
                self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, encode(value)))
                self.db.commit()
                return value
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def invalidate(self):
        with self.lock:
            self.db.execute("UPDATE checks SET status='pending',expires=0,due=0,attempted=0")
            self.db.commit()

    def add_categories(self, catalogs, instance_id):
        """Reconcile the published shelves with the catalogue source. The source is a
        dedicated profile that is never switched over, so its manifest is authoritative:
        shelves it no longer offers are retired rather than left stale."""
        with self.lock:
            retained = set()
            for position, cat in enumerate(catalogs):
                if cat.get('type') not in ('movie', 'series'):
                    continue
                if any(e.get('name') == 'search' and e.get('isRequired') for e in cat.get('extra', [])):
                    continue
                if is_scanner_catalog(cat['id'], instance_id):
                    continue
                cid = 'cached-' + hashlib.sha256((cat['type'] + '/' + cat['id']).encode()).hexdigest()[:16]
                retained.add(cid)
                self.db.execute(
                    '''INSERT INTO categories(id,type,name,upstream,position,extra,active) VALUES (?,?,?,?,?,?,1)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,position=excluded.position,
                       extra=excluded.extra,
                       offset=CASE WHEN categories.active=0 THEN 0 ELSE categories.offset END,
                       done=CASE WHEN categories.active=0 THEN 0 ELSE categories.done END,
                       refresh=CASE WHEN categories.active=0 THEN 0 ELSE categories.refresh END,
                       generation=CASE WHEN categories.active=0 THEN categories.generation+1 ELSE categories.generation END,
                       active=1''',
                    (cid, cat['type'], cat.get('name', cat['id']), cat['id'], position, encode(cat.get('extra', []))))
            for row in self.db.execute('SELECT id FROM categories WHERE active=1').fetchall():
                if row['id'] not in retained:
                    self.db.execute('UPDATE categories SET active=0,generation=generation+1 WHERE id=?', (row['id'],))
            retired = [row[0] for row in self.db.execute('SELECT id FROM categories WHERE active=0').fetchall()]
            if retired:
                marks = ','.join('?' for _ in retired)
                self.db.execute('DELETE FROM membership WHERE category IN (%s)' % marks, retired)
                # Titles and checks that no longer belong to any shelf are dropped.
                self.db.execute('DELETE FROM checks WHERE NOT EXISTS (SELECT 1 FROM membership m WHERE m.type=checks.type AND m.id=checks.parent)')
                self.db.execute('DELETE FROM titles WHERE NOT EXISTS (SELECT 1 FROM membership m WHERE m.type=titles.type AND m.id=titles.id)')
            self.db.commit()

    def add_page(self, cat, previews, offset, cap):
        with self.lock:
            current = self.db.execute('SELECT active,generation FROM categories WHERE id=?', (cat['id'],)).fetchone()
            if current is None or not current['active'] or current['generation'] != cat['generation']:
                # A response may arrive after the category was removed/restored or
                # after another crawl started. It must not revive obsolete members.
                return False
            generation = max(time.time(), current['generation'] + 1) if offset == 0 else cat['generation']
            if offset == 0:
                self.db.execute('UPDATE categories SET generation=? WHERE id=?', (generation, cat['id']))
            new = 0
            for n, p in enumerate(previews):
                if not isinstance(p, dict) or not isinstance(p.get('id'), str):
                    continue
                kind, ident = cat['type'], p['id']
                p = {k: v for k, v in p.items() if k not in ('videos', 'streams')}
                p['type'] = kind
                # Breadth first: the first page of every shelf is indexed before the
                # second, and the curated family shelves come before everything else.
                shelf = -100 if 'familycurated' in cat['upstream'] else offset // 20
                previous = self.db.execute('SELECT meta,search FROM titles WHERE type=? AND id=?', (kind, ident)).fetchone()
                search = self.search_names(p, json.loads(previous['meta'] or '{}') if previous else {},
                                           previous['search'].splitlines() if previous and previous['search'] else [])
                self.db.execute('''INSERT INTO titles(type,id,preview,search,priority,rank) VALUES (?,?,?,?,?,?)
                  ON CONFLICT(type,id) DO UPDATE SET preview=excluded.preview,search=excluded.search,
                  priority=CASE WHEN titles.priority<excluded.priority THEN titles.priority ELSE excluded.priority END,
                  rank=CASE WHEN titles.rank<excluded.rank THEN titles.rank ELSE excluded.rank END''',
                  (kind, ident, encode(p), search, shelf, offset + n))
                seen = self.db.execute('SELECT seen FROM membership WHERE category=? AND type=? AND id=?', (cat['id'], kind, ident)).fetchone()
                new += int(seen is None or seen[0] != generation)
                self.db.execute('''INSERT INTO membership VALUES (?,?,?,?,?) ON CONFLICT(category,type,id)
                  DO UPDATE SET rank=excluded.rank,seen=excluded.seen''', (cat['id'], kind, ident, offset + n, generation))
                # Curated shelves are probed first, then page by page across the rest.
                if kind == 'movie':
                    self.db.execute('INSERT OR IGNORE INTO checks(type,id,parent,priority) VALUES (?,?,?,?)', (kind, ident, ident, shelf))
            skip = any(e.get('name') == 'skip' for e in json.loads(cat['extra']))
            done = not previews or (offset > 0 and not new) or not skip or offset + len(previews) >= cap
            if done:
                self.db.execute('DELETE FROM membership WHERE category=? AND seen!=?', (cat['id'], generation))
            self.db.execute('UPDATE categories SET offset=?,done=?,refresh=? WHERE id=?',
                            (offset + len(previews), int(done), time.time() + 86400 if done else 0, cat['id']))
            self.db.commit()
            return True

    def save_meta(self, kind, ident, meta, now):
        with self.lock:
            title = self.db.execute('''SELECT * FROM titles t WHERE type=? AND id=? AND EXISTS
              (SELECT 1 FROM membership m JOIN categories c ON c.id=m.category
               WHERE m.type=t.type AND m.id=t.id AND c.active=1)''', (kind, ident)).fetchone()
            if title is None:
                return
            videos = []
            valid = isinstance(meta, dict) and isinstance(meta.get('id'), str) and bool(meta['id'])
            if valid and kind == 'series':
                listed = meta.get('videos')
                valid = isinstance(listed, list) and all(isinstance(v, dict) for v in listed)
                if valid:
                    videos = sorted((v for v in listed if isinstance(v.get('id'), str)
                                     and isinstance(v.get('released'), str) and aired(v, now)),
                                    key=lambda v: (v['season'] == 0, v['season'], v['episode'], v['id']))
                    valid = bool(videos)
            if not valid:
                # Empty/malformed metadata is not evidence that existing episodes
                # disappeared. Keep both previous metadata and check results.
                self.db.execute('UPDATE titles SET meta_due=?,meta_claimed=0 WHERE type=? AND id=?',
                                (now + 1800, kind, ident))
                self.db.commit()
                return
            meta = copy.deepcopy(meta)
            meta.pop('streams', None)
            # Retain translated names encountered in previous metadata responses.
            search = self.search_names(json.loads(title['preview']), meta,
                                       json.loads(title['meta'] or '{}'), (title['search'] or '').splitlines())
            self.db.execute('UPDATE titles SET meta=?,meta_due=?,meta_checked=?,meta_claimed=0,search=? WHERE type=? AND id=?',
                            (encode(meta), now + 86400, now, search, kind, ident))
            if kind == 'series':
                # max_episodes controls the worker's batch size, never completeness
                # of the persistent candidate list. Start regular seasons in order;
                # specials follow the main series.
                for position, video in enumerate(videos):
                    self.db.execute('''INSERT INTO checks(type,id,parent,priority) VALUES (?,?,?,?)
                      ON CONFLICT(type,id,parent) DO UPDATE SET priority=excluded.priority''',
                                    (kind, video['id'], ident, position))
                keep = {v['id'] for v in videos}
                for row in self.db.execute('SELECT id FROM checks WHERE type=? AND parent=?', (kind, ident)).fetchall():
                    if row['id'] not in keep:
                        self.db.execute('DELETE FROM checks WHERE type=? AND id=? AND parent=?', (kind, row['id'], ident))
            self.db.commit()

    def record(self, kind, ident, parent, verdict, count, now):
        with self.lock:
            row = self.db.execute('''SELECT * FROM checks c WHERE type=? AND id=? AND parent=? AND EXISTS
              (SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
               WHERE m.type=c.type AND m.id=c.parent AND cat.active=1)''', (kind, ident, parent)).fetchone()
            if row is None:
                return
            if verdict == 'error':
                failures = row['failures'] + 1
                # A failure never extends an earlier positive confirmation.
                self.db.execute('UPDATE checks SET due=?,attempted=?,failures=? WHERE type=? AND id=? AND parent=?',
                                (now + min(3600, 120 * 2 ** min(failures, 5)), now, failures, kind, ident, parent))
            else:
                ttl = self.positive if verdict == 'available' else self.negative
                self.db.execute('''UPDATE checks SET status=?,checked=?,expires=?,due=?,attempted=?,failures=0,count=?
                  WHERE type=? AND id=? AND parent=?''', (verdict, now, now + ttl, now + ttl * .8, now, count, kind, ident, parent))
            self.db.commit()

    def visible(self, kind, ident, now):
        return self.db.execute('''SELECT 1 FROM checks c WHERE type=? AND parent=? AND status='available' AND expires>?
          AND EXISTS (SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
            WHERE m.type=c.type AND m.id=c.parent AND cat.active=1) LIMIT 1''',
                               (kind, ident, now + 60)).fetchone() is not None

    @staticmethod
    def canonical_id(preview, meta):
        """Only merge aliases with an explicit shared identifier, never by title."""
        for document in (meta, preview):
            for field in ('imdb_id', 'imdbId'):
                ident = document.get(field)
                if isinstance(ident, str) and re.fullmatch(r'tt\d+', ident):
                    return ident
            providers = document.get('providerIds') or document.get('ProviderIds') or {}
            if isinstance(providers, dict):
                ident = providers.get('Imdb') or providers.get('imdb') or providers.get('IMDB')
                if isinstance(ident, str) and re.fullmatch(r'tt\d+', ident):
                    return ident
        return meta.get('id') or preview['id']

    def catalog(self, kind, cid, skip=0, search=None, genre=None, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if search is not None:
                rows = self.db.execute('''SELECT t.* FROM titles t WHERE t.type=? AND EXISTS
                  (SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available' AND c.expires>?)
                  AND EXISTS (SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
                    WHERE m.type=t.type AND m.id=t.id AND cat.active=1)
                  ORDER BY t.id''', (kind, now + 60)).fetchall()
            else:
                rows = self.db.execute('''SELECT t.* FROM membership m JOIN titles t ON t.type=m.type AND t.id=m.id
                  JOIN categories cat ON cat.id=m.category WHERE cat.active=1 AND m.category=? AND t.type=? AND EXISTS
                  (SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available' AND c.expires>?)
                  ORDER BY m.rank,t.id''', (cid, kind, now + 60)).fetchall()
            result = []
            seen = set()
            query = folded(search or '')
            for row in rows:
                if query and query not in row['search']:
                    continue
                preview = json.loads(row['preview'])
                meta = json.loads(row['meta'] or '{}')
                genres = sorted(set(x for x in (preview.get('genres') or []) + (meta.get('genres') or []) if isinstance(x, str)))
                if genre and genre not in genres:
                    continue
                canonical = self.canonical_id(preview, meta)
                if canonical in seen:
                    continue
                seen.add(canonical)
                preview.pop('videos', None)
                preview['genres'] = genres
                result.append(preview)
            return result[max(0, skip):max(0, skip) + 100]

    def genres(self, kind, now=None):
        """Genre facets come from the same currently verified pool as catalogues."""
        now = time.time() if now is None else now
        with self.lock:
            rows = self.db.execute('''SELECT t.preview,t.meta FROM titles t WHERE t.type=? AND EXISTS
              (SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
               WHERE m.type=t.type AND m.id=t.id AND cat.active=1) AND EXISTS
              (SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available' AND c.expires>?)''',
                                   (kind, now + 60)).fetchall()
            values = set()
            for row in rows:
                for data in (row['preview'], row['meta']):
                    for genre in json.loads(data or '{}').get('genres') or []:
                        if isinstance(genre, str) and genre.strip():
                            values.add(genre)
            return sorted(values, key=folded)

    def meta(self, kind, ident, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if not self.visible(kind, ident, now):
                return None
            row = self.db.execute('SELECT * FROM titles WHERE type=? AND id=?', (kind, ident)).fetchone()
            if not row:
                return None
            meta = json.loads(row['meta'] or row['preview'])
            if kind == 'series':
                allowed = {r[0] for r in self.db.execute("SELECT id FROM checks WHERE type=? AND parent=? AND status='available' AND expires>?", (kind, ident, now + 60))}
                meta['videos'] = [v for v in meta.get('videos', []) if v.get('id') in allowed and aired(v, now)]
            return meta

    def lookup(self, kind, ident):
        """Diagnostics for one title: why it is or is not published."""
        now = time.time()
        with self.lock:
            title = self.db.execute('SELECT type,id,priority,rank,meta,meta_checked,meta_due,meta_claimed FROM titles WHERE type=? AND id=?',
                                    (kind, ident)).fetchone()
            if title is None:
                return {'known': False}
            memberships = self.db.execute('SELECT category,rank FROM membership WHERE type=? AND id=? ORDER BY rank',
                                          (kind, ident)).fetchall()
            checks = self.db.execute('SELECT status,count(*),min(due),max(expires) FROM checks WHERE type=? AND parent=? GROUP BY status',
                                     (kind, ident)).fetchall()
            episodes = self.db.execute('SELECT count(*) FROM checks WHERE type=? AND parent=?', (kind, ident)).fetchone()[0]
            stored = json.loads(title['meta']) if title['meta'] else {}
            listed = stored.get('videos') or []
            return {'known': True, 'type': title['type'], 'id': title['id'], 'priority': title['priority'],
                    'stored_meta': bool(title['meta']), 'stored_videos': len(listed),
                    'stored_videos_aired': sum(1 for v in listed if aired(v, now)),
                    'rank': title['rank'], 'meta_checked': title['meta_checked'], 'meta_due': title['meta_due'],
                    'meta_claimed': title['meta_claimed'], 'episode_checks': episodes,
                    'memberships': [{'category': m['category'], 'rank': m['rank']} for m in memberships],
                    'checks': [{'status': c[0], 'count': c[1], 'earliest_due': c[2], 'latest_expiry': c[3]} for c in checks],
                    'visible': self.visible(kind, ident, now)}

    def exposed_categories(self):
        """Catalogues worth publishing: still crawling, or proven to contain titles.
        A catalogue that finished with nothing is dropped, so a stale or helper
        entry cannot leave a permanently empty shelf."""
        with self.lock:
            return self.db.execute('''SELECT c.* FROM categories c
              WHERE c.active=1 AND (c.done=0 OR EXISTS(SELECT 1 FROM membership m WHERE m.category=c.id))
              ORDER BY c.position''').fetchall()

    def status(self):
        now = time.time()
        with self.lock:
            categories = []
            for c in self.db.execute('SELECT * FROM categories ORDER BY position').fetchall():
                count = self.db.execute('''SELECT count(*) FROM membership m WHERE category=? AND EXISTS
                  (SELECT 1 FROM checks c WHERE c.type=m.type AND c.parent=m.id AND c.status='available' AND c.expires>?)''',
                  (c['id'], now + 60)).fetchone()[0]
                categories.append({'name': c['name'], 'type': c['type'], 'active': bool(c['active']), 'verified_titles': count,
                                   'candidates': self.db.execute('SELECT count(*) FROM membership WHERE category=?', (c['id'],)).fetchone()[0],
                                   'index_complete': bool(c['done'])})
            return {
                'categories': categories,
                'checks': dict(self.db.execute('SELECT status,count(*) FROM checks GROUP BY status').fetchall()),
                'verified_now': self.db.execute("SELECT count(*) FROM checks WHERE status='available' AND expires>?", (now + 60,)).fetchone()[0],
                'retrying': self.db.execute('SELECT count(*) FROM checks WHERE failures>0').fetchone()[0],
                'pending': self.db.execute("SELECT count(*) FROM checks WHERE due<?", (now,)).fetchone()[0],
                'metadata_pending': self.db.execute("SELECT count(*) FROM titles WHERE meta_due<?", (now,)).fetchone()[0],
            }


class App:
    def __init__(self, options, path):
        self.o = options
        self.store = Store(path, options.get('positive_hours', 12) * 3600, options.get('negative_hours', 24) * 3600,
                           options.get('max_episodes_per_series', 12))
        self.ready = False
        self.error = 'Initialising'
        self.provider_errors_visible = False
        self.policy_lock = threading.RLock()
        self.inflight = set()
        self.check_queue = deque()
        self.stop = threading.Event()
        self.heartbeats = {}
        self.threads = {}
        self.worker_errors = {}
        self.interest_times = {}
        self.pacing_lock = threading.Lock()
        self.next_stream_request = 0.0
        self.cooldown_until = 0.0
        self.instance_id = options.get('scanner_instance_id', 'cachedlibrary')
        self.delay = float(options.get('check_delay_seconds', 2))
        self.base = (options.get('aiostreams_url') or '').rstrip('/')
        self.source = self.stremio(options.get('stremio_uuid'), options.get('stremio_encrypted_password'))
        # Catalogue definitions come from a profile that is never switched over, so a
        # fresh install can still bootstrap after the family catalogues are hidden.
        self.catalog_source = self.stremio(
            options.get('catalog_uuid') or options.get('stremio_uuid'),
            options.get('catalog_encrypted_password') or options.get('stremio_encrypted_password'))
        self.o['endpoint_token'] = self.resolve_token(options.get('endpoint_token'))
        if self.catalog_source is None:
            self.catalog_source = self.source

    def stremio(self, uuid, encrypted):
        if not uuid or not encrypted:
            return None
        return self.base + '/stremio/' + quote(uuid, safe='') + '/' + quote(encrypted, safe='')

    def resolve_token(self, token):
        """Use the configured token, otherwise generate one and remember it, so a
        fresh install can start without hand-crafted secrets."""
        token = (token or '').strip()
        if token:
            return token
        token = self.store.setting('endpoint_token') or secrets.token_urlsafe(32)
        self.store.setting('endpoint_token', token)
        return token

    def config_problems(self):
        problems = []
        if not self.base.startswith(('http://', 'https://')):
            problems.append('aiostreams_url must be an http(s) URL')
        if not self.o.get('active_uuid') or not self.o.get('active_password'):
            problems.append('active_uuid and active_password are required')
        if not self.o.get('stremio_uuid') or not self.o.get('stremio_encrypted_password'):
            problems.append('stremio_uuid and stremio_encrypted_password are required')
        if not self.o.get('catalog_uuid') or not self.o.get('catalog_encrypted_password'):
            problems.append('A separate original catalogue/metadata profile is required')
        elif self.o.get('catalog_uuid') == self.o.get('stremio_uuid'):
            problems.append('Catalogue metadata must not use the filtered playback profile')
        if self.o.get('active_uuid') != self.o.get('stremio_uuid'):
            problems.append('The policy and stream profile UUIDs must match')
        if len(self.o['endpoint_token']) < 24:
            problems.append('endpoint_token must be at least 24 characters when set')
        return problems

    def is_upstream(self, catalog):
        """A browsable upstream catalogue, as opposed to one of our own or a search helper."""
        cid = catalog.get('id')
        if not cid or catalog.get('type') not in ('movie', 'series'):
            return False
        if is_scanner_catalog(cid, self.instance_id):
            return False
        return not any(e.get('name') == 'search' and e.get('isRequired') for e in catalog.get('extra', []))

    def upstream_catalogs(self):
        manifest = self.request('/manifest.json', catalog=True)
        if any(is_scanner_catalog(c.get('id', ''), self.instance_id) for c in manifest.get('catalogs', [])):
            raise ValueError('The original catalogue profile must not include Cached Media Library')
        catalogs = [c for c in manifest.get('catalogs', []) if self.is_upstream(c)]
        return catalogs

    def pace_stream(self):
        interval = 60 / max(1, self.o.get('stream_requests_per_minute', 30))
        while not self.stop.is_set():
            with self.pacing_lock:
                now = time.monotonic()
                wait = max(self.next_stream_request, self.cooldown_until) - now
                if wait <= 0:
                    self.next_stream_request = now + interval
                    return
            self.stop.wait(min(wait, 5))
        raise RuntimeError('Stopping')

    def backoff(self, retry_after=None):
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, AttributeError):
                seconds = 20
        with self.pacing_lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + min(300, max(20, seconds)))

    def request(self, path, config=False, catalog=False):
        """Read-only. `config=True` reads profile settings over the dashboard API,
        `catalog=True` reads from the catalogue source profile."""
        headers = {'User-Agent': 'CachedMediaLibrary/0.1', 'Content-Type': 'application/json'}
        if config:
            raw = self.o['active_uuid'] + ':' + self.o['active_password']
            headers['Authorization'] = 'Basic ' + base64.b64encode(raw.encode()).decode()
            url = self.base + path
        else:
            url = (self.catalog_source if catalog else self.source) + path
        is_stream = path.startswith('/stream/')
        if is_stream:
            self.pace_stream()
        try:
            with urlopen(Request(url, headers=headers), timeout=65) as response:
                return json.load(response)
        except HTTPError as exc:
            if is_stream and exc.code in (429, 503):
                self.backoff(exc.headers.get('Retry-After'))
            raise

    def check_policy(self, config):
        if config.get('parentConfig') or config.get('variants'):
            return 'Inherited or conditional playback policies require a resolved scanner policy'
        if not config.get('excludeUncached'):
            return 'AIOStreams is not excluding uncached streams'
        if not any(e.get('enabled') and e.get('expression', '').strip() == STRICT_FILTER for e in config.get('requiredStreamExpressions', [])):
            return 'AIOStreams is not requiring a cached TorBox stream'
        return None

    def synchronize(self):
        problems = self.config_problems()
        if problems:
            raise ValueError('; '.join(problems))
        config = self.request('/api/v1/user', config=True)['data']['userData']
        problem = self.check_policy(config)
        if problem:
            raise ValueError(problem)
        catalogs = self.upstream_catalogs()
        if not catalogs:
            raise ValueError('no upstream catalogues found in the profile manifest')
        fingerprint = policy_fingerprint(config)
        with self.policy_lock:
            self.provider_errors_visible = not config.get('hideErrors') and 'stream' not in config.get('hideErrorsForResources', [])
            changed = self.store.setting('policy') != fingerprint
            if changed:
                self.ready = False
            self.store.add_categories(catalogs, self.instance_id)
            if changed:
                self.store.invalidate()
                self.store.setting('policy', fingerprint)
                with self.store.lock:
                    self.check_queue.clear()
            self.ready, self.error = True, None
            self.store.setting('last_sync', time.time())

    def sync_loop(self):
        while not self.stop.is_set():
            try:
                self.synchronize()
            except Exception as exc:
                # urllib errors embed the request URL, which carries credentials, so
                # only our own validation text is repeated verbatim.
                if isinstance(exc, HTTPError):
                    detail = ' HTTP ' + str(exc.code)
                elif isinstance(exc, ValueError):
                    detail = ': ' + str(exc)
                else:
                    detail = ''
                self.ready, self.error = False, 'Synchronisation failed (' + type(exc).__name__ + detail + ')'
                print(self.error, flush=True)
            self.heartbeats['sync'] = time.time()
            self.stop.wait(300 if self.ready else 60)

    def crawl_loop(self):
        while not self.stop.is_set():
            self.heartbeats['crawl'] = time.time()
            if not self.ready:
                self.stop.wait(3)
                continue
            with self.store.lock:
                cats = self.store.db.execute('SELECT * FROM categories WHERE active=1 ORDER BY offset,position').fetchall()
            worked = False
            for cat in cats:
                if cat['done'] and cat['refresh'] > time.time():
                    continue
                offset = 0 if cat['done'] else cat['offset']
                path = '/catalog/' + cat['type'] + '/' + quote(cat['upstream'], safe='')
                path += ('/skip=' + str(offset) if offset else '') + '.json'
                try:
                    result = self.request(path, catalog=True)
                    if not isinstance(result.get('metas'), list):
                        raise ValueError('Invalid catalog response')
                    self.store.add_page(cat, result['metas'], offset, self.o.get('max_candidates_per_category', 1000))
                except Exception as exc:
                    with self.store.lock:
                        self.store.db.execute('UPDATE categories SET done=1,refresh=? WHERE id=? AND active=1 AND generation=?',
                                              (time.time() + 600, cat['id'], cat['generation']))
                        self.store.db.commit()
                    print('Catalogue retry scheduled: ' + type(exc).__name__, flush=True)
                worked = True
                self.heartbeats['crawl'] = time.time()
                self.stop.wait(1)
            if not worked:
                self.stop.wait(30)

    def claim_meta(self):
        """Claim one title for metadata fetching. Episode lists can be megabytes for
        long-running shows, so this runs in several threads and must not double-pick."""
        now = time.time()
        with self.store.lock:
            row = self.store.db.execute('''SELECT t.type,t.id FROM titles t WHERE t.meta_due<? AND t.meta_claimed<?
              AND EXISTS(SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category WHERE m.type=t.type AND m.id=t.id AND cat.active=1)
              AND (t.type='series' OR EXISTS(SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available'))
              ORDER BY (t.scan_touched<0) DESC,(t.meta_checked>0),t.priority,t.rank,t.meta_checked,t.id LIMIT 1''', (now, now)).fetchone()
            if row:
                self.store.db.execute('UPDATE titles SET meta_claimed=? WHERE type=? AND id=?',
                                      (now + 900, row['type'], row['id']))
                self.store.db.commit()
            return row

    def prioritize(self, kind, ident=None, search=None):
        """An actual client visit can advance pending work, never its verdict."""
        now = time.time()
        with self.store.lock:
            where, value = ('t.id=?', ident) if ident is not None else ('instr(t.search,?)>0', folded(search or ''))
            if not value:
                return
            rows = self.store.db.execute('''SELECT t.id FROM titles t WHERE t.type=? AND '''+where+'''
              AND EXISTS(SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
                         WHERE m.type=t.type AND m.id=t.id AND cat.active=1)
              ORDER BY t.rank,t.id LIMIT 20''',(kind,value)).fetchall()
            promoted = False
            for row in rows:
                key = (kind,row['id'])
                if self.interest_times.get(key,0) > now-300:
                    continue
                self.interest_times[key] = now
                self.store.db.execute('UPDATE titles SET scan_touched=? WHERE type=? AND id=?',(-now,kind,row['id']))
                promoted = True
            if promoted:
                self.check_queue.clear()
                self.store.db.commit()
            if len(self.interest_times)>1000:
                self.interest_times = {k:v for k,v in self.interest_times.items() if v>now-300}

    def metadata_worker(self, number):
        while not self.stop.is_set():
            self.heartbeats['metadata' + str(number)] = time.time()
            if not self.ready:
                self.stop.wait(3)
                continue
            row = self.claim_meta()
            if row is None:
                self.stop.wait(5)
                continue
            try:
                result = self.request('/meta/' + row['type'] + '/' + quote(row['id'], safe='') + '.json', catalog=True)
                self.store.save_meta(row['type'], row['id'], result.get('meta'), time.time())
            except Exception:
                self.store.save_meta(row['type'], row['id'], None, time.time())
            self.stop.wait(0.2)

    def next_check(self, preferred):
        with self.store.lock:
            now = time.time()
            # Indexed urgent renewals, independent of the size of the episode backlog.
            rows = self.store.db.execute('''SELECT c.* FROM checks c
              WHERE c.status='available' AND c.due<? AND EXISTS
              (SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
               WHERE m.type=c.type AND m.id=c.parent AND cat.active=1)
              ORDER BY c.due LIMIT 100''', (now,)).fetchall()
            for row in rows:
                key = (row['type'], row['id'], row['parent'])
                if key not in self.inflight:
                    self.inflight.add(key)
                    return row
            while True:
                while self.check_queue:
                    queued = self.check_queue.popleft()
                    row = self.store.db.execute('''SELECT c.* FROM checks c WHERE c.type=? AND c.id=? AND c.parent=?
                      AND c.due<? AND EXISTS(SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
                      WHERE m.type=c.type AND m.id=c.parent AND cat.active=1)''', (*queued, now)).fetchone()
                    if row is not None and queued not in self.inflight:
                        self.inflight.add(queued)
                        return row
                title = self.store.db.execute('''SELECT t.type,t.id FROM titles t
                  WHERE EXISTS(SELECT 1 FROM membership m JOIN categories cat ON cat.id=m.category
                    WHERE m.type=t.type AND m.id=t.id AND cat.active=1)
                  AND EXISTS(SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.due<?)
                  ORDER BY (t.type!=?),(t.scan_touched>0),t.scan_touched,t.priority,t.rank,t.id LIMIT 1''',
                  (now, preferred)).fetchone()
                if title is None:
                    return None
                rows = self.store.db.execute('''SELECT type,id,parent FROM checks WHERE type=? AND parent=? AND due<?
                  ORDER BY (attempted>0),priority,due,id LIMIT ?''',
                  (title['type'], title['id'], now, self.o.get('max_episodes_per_series', 12))).fetchall()
                self.store.db.execute('UPDATE titles SET scan_touched=? WHERE type=? AND id=?', (now, title['type'], title['id']))
                self.store.db.commit()
                self.check_queue.extend((r['type'],r['id'],r['parent']) for r in rows
                                        if (r['type'],r['id'],r['parent']) not in self.inflight)
                if not self.check_queue:
                    # All currently eligible work for this parent is already in flight.
                    return None

    def worker(self, number):
        turn = number
        while not self.stop.is_set():
            self.heartbeats['worker' + str(number)] = time.time()
            if not self.ready:
                self.stop.wait(3)
                continue
            row = self.next_check('movie' if turn % 2 == 0 else 'series')
            turn += 1
            if row is None:
                self.stop.wait(3)
                continue
            try:
                generation = self.store.setting('policy')
                try:
                    result = self.request('/stream/' + row['type'] + '/' + quote(row['id'], safe='') + '.json')
                    verdict, count = stream_verdict(result)
                    if verdict == 'unavailable' and not self.provider_errors_visible:
                        # A filtering card may describe one provider while another
                        # provider's failure was hidden by the playback profile.
                        verdict = 'error'
                except Exception:
                    verdict, count = 'error', 0
                with self.policy_lock:
                    if self.ready and generation == self.store.setting('policy'):
                        self.store.record(row['type'], row['id'], row['parent'], verdict, count, time.time())
            finally:
                with self.store.lock:
                    self.inflight.discard((row['type'], row['id'], row['parent']))
            self.stop.wait(self.delay)

    def manifest(self):
        catalogs = []
        genres = {kind: self.store.genres(kind) for kind in ('movie', 'series')}
        for cat in self.store.exposed_categories():
            extras = [{'name': 'skip', 'isRequired': False}]
            if genres[cat['type']]:
                extras.append({'name': 'genre', 'isRequired': False, 'options': genres[cat['type']]})
            catalogs.append({'id': cat['id'], 'type': cat['type'], 'name': cat['name'], 'extra': extras})
        for kind in ('movie', 'series'):
            catalogs.append({'id': 'cached-search', 'type': kind, 'name': 'Available ' + kind + ' search',
                             'extra': [{'name': 'search', 'isRequired': True}, {'name': 'skip', 'isRequired': False}]})
        return {'id': 'local.cached.media.library', 'version': '0.7.1', 'name': 'Cached Media Library',
                'description': 'Recently verified cached streams matching your AIOStreams filters. Metadata only.',
                'types': ['movie', 'series'],
                'resources': ['catalog', {'name': 'meta', 'types': ['movie', 'series'], 'idPrefixes': ['tt', 'tmdb:']}],
                'catalogs': catalogs, 'behaviorHints': {'configurable': False}, 'cacheMaxAge': 30}

    def guarded(self, name, function):
        while not self.stop.is_set():
            try:
                function()
            except Exception as exc:
                self.worker_errors[name] = {'type': type(exc).__name__, 'at': time.time()}
                print('Restarting '+name+' after '+type(exc).__name__, flush=True)
            if not self.stop.is_set():
                self.stop.wait(5)

    def start(self):
        threads = [('sync', self.sync_loop), ('crawl', self.crawl_loop)]
        threads += [('metadata' + str(i), lambda i=i: self.metadata_worker(i)) for i in range(self.o.get('metadata_workers', 2))]
        threads += [('worker' + str(i), lambda i=i: self.worker(i)) for i in range(self.o.get('workers', 2))]
        for name, func in threads:
            self.heartbeats[name] = time.time()
            thread = threading.Thread(target=self.guarded, args=(name, func), name=name, daemon=True)
            self.threads[name] = thread
            thread.start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Request paths carry the private endpoint token.

    def send(self, value, status=200):
        data = encode(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        app = self.server.app
        split = urlsplit(self.path)
        path = split.path
        if path in ('/', '/health', '/status') and parse_qs(split.query).get('lookup'):
            ident = parse_qs(split.query)['lookup'][0]
            kind = 'series'
            if ':' in ident and ident.split(':', 1)[0] in ('movie', 'series'):
                kind, ident = ident.split(':', 1)
            found = app.store.lookup(kind, ident)
            if not found.get('known') and kind == 'series':
                found = app.store.lookup('movie', ident)
            self.send({'lookup': found})
            return
        if path in ('/', '/health', '/status'):
            status = {'ready': app.ready, 'issue': app.error, 'last_policy_sync': app.store.setting('last_sync'),
                      'worker_errors': app.worker_errors,
                      'workers_alive': {k: v.is_alive() for k,v in app.threads.items()},
                      'worker_ages': {k: round(time.time() - v) for k, v in app.heartbeats.items()}, **app.store.status()}
            unhealthy = not app.ready or any(v > 420 for v in status['worker_ages'].values())
            self.send(status, 503 if path == '/health' and unhealthy else 200)
            return
        parts = path.strip('/').split('/')
        if len(parts) < 2 or not hmac.compare_digest(parts[0], app.o['endpoint_token']):
            self.send({'error': 'Not found'}, 404)
            return
        if not app.ready:
            self.send({'error': 'Availability index is synchronising', 'cacheMaxAge': 0}, 503)
            return
        if parts[1] == 'manifest.json':
            self.send(app.manifest())
            return
        if len(parts) not in (4, 5) or parts[2] not in ('movie', 'series') or not parts[-1].endswith('.json'):
            self.send({'error': 'Not found'}, 404)
            return
        kind = parts[2]
        ident = unquote(parts[3].removesuffix('.json') if len(parts) == 4 else parts[3])
        if parts[1] == 'meta':
            app.prioritize(kind,ident=ident)
            self.send({'meta': app.store.meta(kind, ident), 'cacheMaxAge': 30, 'staleRevalidate': 0, 'staleError': 0})
            return
        if parts[1] != 'catalog':
            self.send({'error': 'Not found'}, 404)
            return
        extra = parse_qs(parts[4][:-5]) if len(parts) == 5 else {}
        try:
            skip = max(0, int(extra.get('skip', ['0'])[0]))
        except ValueError:
            skip = 0
        search = extra.get('search', [None])[0]
        if ident == 'cached-search' and search:
            app.prioritize(kind,search=search)
        if ident == 'cached-search' and not search:
            metas = []
        else:
            metas = app.store.catalog(kind, ident, skip, search if ident == 'cached-search' else None, extra.get('genre', [None])[0])
        self.send({'metas': metas, 'cacheMaxAge': 30, 'staleRevalidate': 0, 'staleError': 0})


if __name__ == '__main__':
    with open(os.environ.get('OPTIONS_PATH', '/data/options.json')) as f:
        options = json.load(f)
    try:
        app = App(options, os.environ.get('DATABASE_PATH', '/data/availability.sqlite'))
    except Exception as exc:  # Never dump a traceback for a configuration problem.
        print('Cached Media Library cannot start: ' + type(exc).__name__ + ': ' + str(exc), flush=True)
        raise SystemExit(1)
    app.start()
    server = ThreadingHTTPServer(('0.0.0.0', 8097), Handler)
    server.app = app
    print('Cached Media Library listening; metadata only', flush=True)
    print('Add this manifest URL to AIOStreams as a custom add-on:', flush=True)
    print('  http://HOME_ASSISTANT_HOST:8097/' + app.o['endpoint_token'] + '/manifest.json', flush=True)
    server.serve_forever()
