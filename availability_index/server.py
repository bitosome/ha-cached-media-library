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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

# Settings that decide whether a stream counts as playable. A change to any of
# them invalidates stored verdicts; preferences such as sort order do not.
POLICY_KEYS = (
    'excludeUncached',
    'requiredStreamExpressions',
    'excludedQualities',
    'excludedVisualTags',
    'services',
    'titleMatching',
    'seasonEpisodeMatching',
    'checkOwned',
)
STRICT_FILTER = "cached(service(streams, 'torbox'))"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def folded(value):
    return unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')


def playable(stream):
    # Information cards, error cards, magnets and P2P hashes are not playable.
    return isinstance(stream, dict) and urlsplit(stream.get('url') or '').scheme in ('http', 'https')


def stream_verdict(result):
    streams = result.get('streams')
    if not isinstance(streams, list):
        return 'error', 0
    count = sum(playable(s) for s in streams)
    if count:
        return 'available', count
    notices = ' '.join(str(s.get('description', '')) + ' ' + str(s.get('name', '')) for s in streams)
    if result.get('error') or re.search(r'timeout|timed out|rate.?limit|unavailable|failed|error|429', notices, re.I):
        return 'error', 0
    return 'unavailable', 0


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
    return hashlib.sha256(encode({k: config.get(k) for k in POLICY_KEYS}).encode()).hexdigest()


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
          CREATE TABLE IF NOT EXISTS titles (type TEXT,id TEXT,preview TEXT,meta TEXT,search TEXT,meta_due REAL DEFAULT 0,meta_checked REAL DEFAULT 0,priority INTEGER DEFAULT 9999,meta_claimed REAL DEFAULT 0,PRIMARY KEY(type,id));
          CREATE TABLE IF NOT EXISTS membership (category TEXT,type TEXT,id TEXT,rank INTEGER,seen REAL DEFAULT 0,PRIMARY KEY(category,type,id));
          CREATE TABLE IF NOT EXISTS checks (type TEXT,id TEXT,parent TEXT,status TEXT DEFAULT 'pending',checked REAL DEFAULT 0,expires REAL DEFAULT 0,due REAL DEFAULT 0,attempted REAL DEFAULT 0,failures INTEGER DEFAULT 0,count INTEGER DEFAULT 0,priority INTEGER DEFAULT 0,PRIMARY KEY(type,id));
          CREATE INDEX IF NOT EXISTS due_checks ON checks(due,priority);
          CREATE INDEX IF NOT EXISTS parent_checks ON checks(type,parent,status,expires);
          CREATE INDEX IF NOT EXISTS title_memberships ON membership(type,id);
        ''')
        self.migrate()
        self.db.commit()

    def migrate(self):
        """Add columns introduced after a database was first created."""
        wanted = {'titles': [('priority', 'INTEGER DEFAULT 9999'), ('meta_claimed', 'REAL DEFAULT 0')],
                  'categories': [('active', 'INTEGER DEFAULT 1')]}
        for table, columns in wanted.items():
            existing = {row[1] for row in self.db.execute('PRAGMA table_info(%s)' % table)}
            for name, ddl in columns:
                if name not in existing:
                    self.db.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, ddl))

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
            self.db.execute('UPDATE categories SET active=0')
            for position, cat in enumerate(catalogs):
                if cat.get('type') not in ('movie', 'series'):
                    continue
                if any(e.get('name') == 'search' and e.get('isRequired') for e in cat.get('extra', [])):
                    continue
                if is_scanner_catalog(cat['id'], instance_id):
                    continue
                cid = 'cached-' + hashlib.sha256((cat['type'] + '/' + cat['id']).encode()).hexdigest()[:16]
                self.db.execute(
                    '''INSERT INTO categories(id,type,name,upstream,position,extra,active) VALUES (?,?,?,?,?,?,1)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,position=excluded.position,
                       extra=excluded.extra,active=1''',
                    (cid, cat['type'], cat.get('name', cat['id']), cat['id'], position, encode(cat.get('extra', []))))
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
            generation = time.time() if offset == 0 else cat['generation']
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
                self.db.execute('''INSERT INTO titles(type,id,preview,search,priority) VALUES (?,?,?,?,?)
                  ON CONFLICT(type,id) DO UPDATE SET preview=excluded.preview,search=excluded.search,
                  priority=CASE WHEN titles.priority<excluded.priority THEN titles.priority ELSE excluded.priority END''',
                  (kind, ident, encode(p), folded(p.get('name', '')), shelf))
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

    def save_meta(self, kind, ident, meta, now):
        with self.lock:
            if not isinstance(meta, dict) or not meta.get('id'):
                self.db.execute('UPDATE titles SET meta_due=? WHERE type=? AND id=?', (now + 1800, kind, ident))
                self.db.commit()
                return
            meta = copy.deepcopy(meta)
            meta.pop('streams', None)
            self.db.execute('UPDATE titles SET meta=?,meta_due=?,meta_checked=?,search=search || ? WHERE type=? AND id=?',
                            (encode(meta), now + 86400, now, ' ' + folded(meta.get('name', '')), kind, ident))
            if kind == 'series':
                videos = sorted((v for v in meta.get('videos', []) if aired(v, now)), key=episode_order)[:self.max_episodes]
                curated = self.db.execute("SELECT 1 FROM membership m JOIN categories c ON c.id=m.category WHERE m.type=? AND m.id=? AND c.upstream LIKE 'familycurated%' LIMIT 1", (kind, ident)).fetchone()
                for video in videos:
                    self.db.execute('INSERT OR IGNORE INTO checks(type,id,parent,priority) VALUES (?,?,?,?)',
                                    (kind, video['id'], ident, (-100 if curated else 0) if video['episode'] == 1 else 10))
                keep = {v['id'] for v in videos}
                for row in self.db.execute('SELECT id FROM checks WHERE type=? AND parent=?', (kind, ident)).fetchall():
                    if row['id'] not in keep:
                        self.db.execute('DELETE FROM checks WHERE type=? AND id=?', (kind, row['id']))
            self.db.commit()

    def record(self, kind, ident, verdict, count, now):
        with self.lock:
            row = self.db.execute('SELECT * FROM checks WHERE type=? AND id=?', (kind, ident)).fetchone()
            if row is None:
                return
            if verdict == 'error':
                failures = row['failures'] + 1
                # A failure never extends an earlier positive confirmation.
                self.db.execute('UPDATE checks SET due=?,attempted=?,failures=? WHERE type=? AND id=?',
                                (now + min(3600, 120 * 2 ** min(failures, 5)), now, failures, kind, ident))
            else:
                ttl = self.positive if verdict == 'available' else self.negative
                self.db.execute('''UPDATE checks SET status=?,checked=?,expires=?,due=?,attempted=?,failures=0,count=?
                  WHERE type=? AND id=?''', (verdict, now, now + ttl, now + ttl * .8, now, count, kind, ident))
            self.db.commit()

    def visible(self, kind, ident, now):
        return self.db.execute("SELECT 1 FROM checks WHERE type=? AND parent=? AND status='available' AND expires>? LIMIT 1",
                               (kind, ident, now + 60)).fetchone() is not None

    def catalog(self, kind, cid, skip=0, search=None, genre=None, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if search is not None:
                rows = self.db.execute('''SELECT t.* FROM titles t WHERE t.type=? AND EXISTS
                  (SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available' AND c.expires>?)
                  AND EXISTS (SELECT 1 FROM membership m WHERE m.type=t.type AND m.id=t.id)
                  ORDER BY t.id''', (kind, now + 60)).fetchall()
            else:
                rows = self.db.execute('''SELECT t.* FROM membership m JOIN titles t ON t.type=m.type AND t.id=m.id
                  WHERE m.category=? AND t.type=? AND EXISTS
                  (SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available' AND c.expires>?)
                  ORDER BY m.rank,t.id''', (cid, kind, now + 60)).fetchall()
            result = []
            query = folded(search or '')
            for row in rows:
                if query and query not in row['search']:
                    continue
                preview = json.loads(row['preview'])
                if genre and genre not in preview.get('genres', []) and genre not in json.loads(row['meta'] or '{}').get('genres', []):
                    continue
                preview.pop('videos', None)
                result.append(preview)
            return result[max(0, skip):max(0, skip) + 100]

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
        self.policy_lock = threading.RLock()
        self.inflight = set()
        self.stop = threading.Event()
        self.heartbeats = {}
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
        catalogs = [c for c in self.request('/manifest.json', catalog=True).get('catalogs', []) if self.is_upstream(c)]
        if not catalogs and self.catalog_source != self.source:
            catalogs = [c for c in self.request('/manifest.json').get('catalogs', []) if self.is_upstream(c)]
        return catalogs

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
        with urlopen(Request(url, headers=headers), timeout=65) as response:
            return json.load(response)

    def check_policy(self, config):
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
            changed = self.store.setting('policy') != fingerprint
            if changed:
                self.ready = False
            self.store.add_categories(catalogs, self.instance_id)
            if changed:
                self.store.invalidate()
                self.store.setting('policy', fingerprint)
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
                        self.store.db.execute('UPDATE categories SET done=1,refresh=? WHERE id=?', (time.time() + 600, cat['id']))
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
            row = self.store.db.execute('''SELECT t.* FROM titles t WHERE t.meta_due<? AND t.meta_claimed<?
              AND EXISTS(SELECT 1 FROM membership m WHERE m.type=t.type AND m.id=t.id)
              AND (t.type='series' OR EXISTS(SELECT 1 FROM checks c WHERE c.type=t.type AND c.parent=t.id AND c.status='available'))
              ORDER BY (t.meta_checked>0),t.priority,t.meta_checked,t.id LIMIT 1''', (now, now)).fetchone()
            if row:
                self.store.db.execute('UPDATE titles SET meta_claimed=? WHERE type=? AND id=?',
                                      (now + 900, row['type'], row['id']))
                self.store.db.commit()
            return row

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
                result = self.request('/meta/' + row['type'] + '/' + quote(row['id'], safe='') + '.json')
                self.store.save_meta(row['type'], row['id'], result.get('meta'), time.time())
            except Exception:
                self.store.save_meta(row['type'], row['id'], None, time.time())
            self.stop.wait(0.2)

    def next_check(self, preferred):
        with self.store.lock:
            # Refresh positives before they expire, then work the backlog.
            rows = self.store.db.execute('''SELECT c.* FROM checks c WHERE due<? AND EXISTS
              (SELECT 1 FROM membership m WHERE m.type=c.type AND m.id=c.parent)
              ORDER BY CASE WHEN status='available' THEN 0 ELSE 1 END,(c.type!=?),priority,
              (SELECT max(c2.attempted) FROM checks c2 WHERE c2.type=c.type AND c2.parent=c.parent),c.attempted,c.id LIMIT 100''',
              (time.time(), preferred)).fetchall()
            for row in rows:
                key = (row['type'], row['id'])
                if key not in self.inflight:
                    self.inflight.add(key)
                    return row

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
            generation = self.store.setting('policy')
            try:
                result = self.request('/stream/' + row['type'] + '/' + quote(row['id'], safe='') + '.json')
                verdict, count = stream_verdict(result)
            except HTTPError as exc:
                verdict, count = 'error', 0
                if exc.code in (429, 503):  # Shared per-IP limiter: back off globally.
                    self.stop.wait(20)
            except Exception:
                verdict, count = 'error', 0
            with self.policy_lock:
                if self.ready and generation == self.store.setting('policy'):
                    self.store.record(row['type'], row['id'], verdict, count, time.time())
            with self.store.lock:
                self.inflight.discard((row['type'], row['id']))
            self.stop.wait(self.delay)

    def manifest(self):
        catalogs = []
        for cat in self.store.exposed_categories():
            catalogs.append({'id': cat['id'], 'type': cat['type'], 'name': cat['name'], 'extra': [{'name': 'skip', 'isRequired': False}]})
        for kind in ('movie', 'series'):
            catalogs.append({'id': 'cached-search', 'type': kind, 'name': 'Available ' + kind + ' search',
                             'extra': [{'name': 'search', 'isRequired': True}, {'name': 'skip', 'isRequired': False}]})
        return {'id': 'local.cached.media.library', 'version': '0.5.0', 'name': 'Cached Media Library',
                'description': 'Recently verified cached streams matching your AIOStreams filters. Metadata only.',
                'types': ['movie', 'series'],
                'resources': ['catalog', {'name': 'meta', 'types': ['movie', 'series'], 'idPrefixes': ['tt', 'tmdb:']}],
                'catalogs': catalogs, 'behaviorHints': {'configurable': False}, 'cacheMaxAge': 30}

    def start(self):
        threads = [('sync', self.sync_loop), ('crawl', self.crawl_loop)]
        threads += [('metadata' + str(i), lambda i=i: self.metadata_worker(i)) for i in range(self.o.get('metadata_workers', 2))]
        threads += [('worker' + str(i), lambda i=i: self.worker(i)) for i in range(self.o.get('workers', 2))]
        for name, func in threads:
            self.heartbeats[name] = time.time()
            threading.Thread(target=func, name=name, daemon=True).start()


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
        path = urlsplit(self.path).path
        if path in ('/', '/health', '/status'):
            status = {'ready': app.ready, 'issue': app.error, 'last_policy_sync': app.store.setting('last_sync'),
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
