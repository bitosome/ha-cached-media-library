"""Direct TMDB discovery for locally owned catalogue definitions.

This adapter only requests metadata from TMDB's official API. It never requests
streams. Catalogue identities stay configurable so replacing a catalogue service
does not rename the index's shelves or discard their existing confirmations.
"""
import copy
import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


GENRES = {
    12: 'Adventure', 14: 'Fantasy', 16: 'Animation', 18: 'Drama', 27: 'Horror',
    28: 'Action', 35: 'Comedy', 36: 'History', 37: 'Western', 53: 'Thriller',
    80: 'Crime', 99: 'Documentary', 878: 'Science Fiction', 9648: 'Mystery',
    10402: 'Music', 10749: 'Romance', 10751: 'Family', 10752: 'War',
    10759: 'Action & Adventure', 10762: 'Kids', 10763: 'News', 10764: 'Reality',
    10765: 'Sci-Fi & Fantasy', 10766: 'Soap', 10767: 'Talk',
    10768: 'War & Politics', 10770: 'TV Movie',
}


class TMDBError(RuntimeError):
    """A credential-free, safe-to-log failure; never includes remote bodies."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, never forward a Bearer token to another host.
        return None


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _numbers(value, minimum=1, maximum=100000):
    return (isinstance(value, list) and len(value) <= 100
            and all(_integer(item, minimum) and item <= maximum for item in value))


class TMDBDiscovery:
    PAGE_SIZE = 20
    MAX_PAGE = 500
    FILTERS = frozenset({'sortBy', 'imdbOnly', 'listType', 'includeAdult',
                         'releaseTypes', 'releasedOnly', 'voteCountMin',
                         'ratingMin', 'ratingMax', 'genres', 'excludeGenres',
                         'genreMatchMode', 'tvType', 'releasedWithinDays', 'region'})
    NOW_PLAYING_FILTERS = frozenset({'listType', 'includeAdult', 'releasedOnly', 'region'})

    def __init__(self, api_key, catalogs, language='en-US', request_json=None,
                 today=None, resolve_identity=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError('A TMDB API key or Read Access Token is required')
        if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2}(?:-[A-Z]{2})?', language):
            raise ValueError('Invalid TMDB metadata language')
        if not isinstance(catalogs, list) or not catalogs or len(catalogs) > 100:
            raise ValueError('TMDB catalogues must be a nonempty list of at most 100 definitions')
        self._credential = api_key.strip()
        self.language = language
        self._today = today or (lambda: datetime.now(timezone.utc).date().isoformat())
        self._opener = build_opener(_NoRedirect())
        self._pace_lock = threading.Lock()
        self._next_request = 0.0
        self._request_json = request_json or self._http_json
        self._resolve_identity = resolve_identity
        self._identities = {}
        self._catalogs = {}
        for definition in catalogs:
            self._validate_catalog(definition)
            key = (definition['type'], definition['id'])
            if key in self._catalogs:
                raise ValueError('Duplicate TMDB catalogue identity')
            self._catalogs[key] = copy.deepcopy(definition)

    @classmethod
    def _validate_catalog(cls, value):
        if not isinstance(value, dict) or value.get('type') not in ('movie', 'series'):
            raise ValueError('Invalid TMDB catalogue type')
        for field in ('id', 'name'):
            if not isinstance(value.get(field), str) or not value[field].strip() or len(value[field]) > 250:
                raise ValueError('Invalid TMDB catalogue identity or name')
        filters = value.get('filters', {})
        if not isinstance(filters, dict) or set(filters) - cls.FILTERS:
            raise ValueError('Unsupported TMDB catalogue filters')
        list_type = filters.get('listType', 'discover')
        if list_type not in ('discover', 'now_playing'):
            raise ValueError('Unsupported TMDB list type')
        for field in ('includeAdult', 'releasedOnly'):
            if field in filters and not isinstance(filters[field], bool):
                raise ValueError('Invalid TMDB boolean filter')
        if 'region' in filters and (not isinstance(filters['region'], str)
                                    or not re.fullmatch(r'[A-Z]{2}', filters['region'])):
            raise ValueError('TMDB region must be an uppercase two-letter country code')
        if list_type == 'now_playing':
            if value['type'] != 'movie':
                raise ValueError('TMDB now-playing lists support movies only')
            if set(filters) - cls.NOW_PLAYING_FILTERS:
                raise ValueError('Unsupported filters for the TMDB now-playing list')
            if filters.get('includeAdult', False) or not filters.get('releasedOnly', True):
                raise ValueError('TMDB now-playing lists require released, non-adult movies')
            return
        if 'region' in filters:
            raise ValueError('TMDB region is supported only for now-playing lists')
        if filters.get('imdbOnly', False) is not False:
            raise ValueError('TMDB discover lists require optional IMDb identities')
        if 'releasedWithinDays' in filters:
            days = filters['releasedWithinDays']
            if not _integer(days, 1) or days > 3650:
                raise ValueError('TMDB release window must be an integer from 1 to 3650 days')
            if not filters.get('releasedOnly', True):
                raise ValueError('A TMDB release window requires releasedOnly')
        sorts = {'popularity.desc', 'popularity.asc', 'vote_average.desc', 'vote_average.asc',
                 'vote_count.desc', 'vote_count.asc'}
        sorts |= ({'primary_release_date.desc', 'primary_release_date.asc', 'title.asc', 'title.desc'}
                  if value['type'] == 'movie' else {'first_air_date.desc', 'first_air_date.asc', 'name.asc', 'name.desc'})
        if filters.get('sortBy', 'popularity.desc') not in sorts:
            raise ValueError('Invalid TMDB sort order for the catalogue type')
        for field in ('ratingMin', 'ratingMax'):
            if field in filters and (isinstance(filters[field], bool) or not isinstance(filters[field], (int, float))
                                     or not 0 <= filters[field] <= 10):
                raise ValueError('Invalid TMDB rating filter')
        if filters.get('ratingMin', 0) > filters.get('ratingMax', 10):
            raise ValueError('Invalid TMDB rating range')
        if 'voteCountMin' in filters and not _integer(filters['voteCountMin']):
            raise ValueError('Invalid TMDB vote count filter')
        for field in ('genres', 'excludeGenres'):
            if field in filters and not _numbers(filters[field]):
                raise ValueError('Invalid TMDB genre filter')
        if filters.get('genreMatchMode', 'any') not in ('any', 'all'):
            raise ValueError('Invalid TMDB genre matching mode')
        if 'releaseTypes' in filters and (value['type'] != 'movie' or not _numbers(filters['releaseTypes'], 1, 6)):
            raise ValueError('Invalid TMDB release types')
        if 'tvType' in filters and (value['type'] != 'series' or not isinstance(filters['tvType'], str)
                                    or not re.fullmatch(r'[0-6](?:[|,][0-6])*', filters['tvType'])):
            raise ValueError('Invalid TMDB series type filter')

    def manifest_catalogs(self):
        return [{'id': catalog['id'], 'type': catalog['type'], 'name': catalog['name'],
                 'extra': [{'name': 'skip'}]} for catalog in self._catalogs.values()]

    def has_catalog(self, kind, ident):
        return (kind, ident) in self._catalogs

    def _http_json(self, path, params):
        # Only internal fixed paths reach this method; no host is configurable.
        headers = {'Accept': 'application/json', 'User-Agent': 'CachedMediaLibrary/TMDB'}
        query = dict(params)
        if re.fullmatch(r'[a-fA-F0-9]{32}', self._credential):
            # TMDB v3 keys support query authentication, not Bearer. Keep this
            # URL entirely inside the transport and never repeat it in errors.
            query['api_key'] = self._credential
        else:
            headers['Authorization'] = 'Bearer ' + self._credential
        request = Request('https://api.themoviedb.org/3' + path + '?' + urlencode(query), headers=headers)
        with self._pace_lock:
            wait = self._next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_request = time.monotonic() + 0.2
        try:
            with self._opener.open(request, timeout=30) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise TMDBError('TMDB request failed (HTTP ' + str(exc.code) + ')') from None
        except Exception:
            raise TMDBError('TMDB request failed') from None
        return data

    def _fetch(self, path, params):
        try:
            result = self._request_json(path, params)
        except TMDBError:
            raise
        except Exception:
            raise TMDBError('TMDB request failed') from None
        if not isinstance(result, dict) or result.get('success') is False or result.get('status_code'):
            raise TMDBError('TMDB returned an invalid response')
        return result

    def external_id(self, kind, tmdb_id):
        """Return a confirmed IMDb ID or None; errors are never cached as absence."""
        if kind not in ('movie', 'series') or not _integer(tmdb_id, 1):
            raise ValueError('Invalid TMDB identity lookup')
        media = 'movie' if kind == 'movie' else 'tv'
        result = self._fetch('/' + media + '/' + str(tmdb_id) + '/external_ids', {})
        if not _integer(result.get('id'), 1) or result['id'] != tmdb_id or 'imdb_id' not in result:
            raise TMDBError('TMDB returned invalid external identities')
        ident = result['imdb_id']
        if ident in (None, ''):
            return None
        if not isinstance(ident, str) or not re.fullmatch(r'tt\d{7,12}', ident):
            raise TMDBError('TMDB returned invalid external identities')
        return ident

    def _identity(self, kind, tmdb_id):
        if self._resolve_identity is not None:
            ident = self._resolve_identity(kind, tmdb_id)
        else:
            key = (kind, tmdb_id)
            cached = self._identities.get(key)
            if cached is not None and cached[1] > time.time():
                return cached[0]
            ident = self.external_id(kind, tmdb_id)
            self._identities[key] = (ident, time.time() + (7 * 86400 if ident else 86400))
        if ident is not None and (not isinstance(ident, str) or not re.fullmatch(r'tt\d{7,12}', ident)):
            raise TMDBError('Invalid cached TMDB identity')
        return ident

    def _parameters(self, catalog, page):
        filters = catalog.get('filters', {})
        if filters.get('listType') == 'now_playing':
            # These are the endpoint's only query options. Its own theatrical
            # calendar supplies the date and release-type selection.
            params = {'page': page, 'language': self.language}
            if 'region' in filters:
                params['region'] = filters['region']
            return params
        params = {'page': page, 'language': self.language,
                  'sort_by': filters.get('sortBy', 'popularity.desc'),
                  'include_adult': str(filters.get('includeAdult', False)).lower()}
        today = self._today() if filters.get('releasedOnly', True) else None
        if 'releasedWithinDays' in filters:
            start = (date.fromisoformat(today) - timedelta(days=filters['releasedWithinDays'])).isoformat()
            date_key = 'primary_release_date' if catalog['type'] == 'movie' else 'first_air_date'
            params[date_key + '.gte'] = start
        for field, target in (('ratingMin', 'vote_average.gte'), ('ratingMax', 'vote_average.lte'),
                              ('voteCountMin', 'vote_count.gte')):
            if field in filters:
                params[target] = filters[field]
        for field, target in (('genres', 'with_genres'), ('excludeGenres', 'without_genres')):
            if filters.get(field):
                separator = ',' if field == 'genres' and filters.get('genreMatchMode') == 'all' else '|'
                params[target] = separator.join(map(str, filters[field]))
        if catalog['type'] == 'movie':
            params['include_video'] = 'false'
            if filters.get('releaseTypes'):
                params['with_release_type'] = '|'.join(map(str, filters['releaseTypes']))
            if filters.get('releasedOnly', True):
                params['primary_release_date.lte'] = today
        else:
            params['include_null_first_air_dates'] = 'false'
            if filters.get('tvType'):
                params['with_type'] = filters['tvType']
            if filters.get('releasedOnly', True):
                params['first_air_date.lte'] = today
                params['with_status'] = '0|3|4|5'
        return params

    def _preview(self, kind, item):
        if not isinstance(item, dict) or not _integer(item.get('id'), 1):
            raise TMDBError('TMDB returned an invalid catalogue item')
        name = item.get('title' if kind == 'movie' else 'name')
        if not isinstance(name, str) or not name.strip():
            raise TMDBError('TMDB returned an invalid catalogue title')
        genre_ids = item.get('genre_ids', [])
        if not _numbers(genre_ids):
            raise TMDBError('TMDB returned invalid catalogue genres')
        imdb_id = self._identity(kind, item['id'])
        preview = {'id': imdb_id or 'tmdb:' + str(item['id']), 'tmdbId': item['id'],
                   'type': kind, 'name': name, 'posterShape': 'poster',
                   'description': item.get('overview') or '',
                   'genres': [GENRES[ident] for ident in genre_ids if ident in GENRES]}
        if imdb_id:
            preview['imdb_id'] = imdb_id
            preview['imdbId'] = imdb_id
        released = item.get('release_date' if kind == 'movie' else 'first_air_date')
        if isinstance(released, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', released):
            preview['releaseInfo'] = released[:4]
        for source, target, size in (('poster_path', 'poster', 'w500'), ('backdrop_path', 'background', 'w1280')):
            path = item.get(source)
            if isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png|webp)', path):
                preview[target] = 'https://image.tmdb.org/t/p/' + size + path
        return preview

    @staticmethod
    def _released_cinema_item(item, today):
        # TMDB's theatrical calendar can extend into next week. Keep that list
        # for regional reissues, but enforce our releasedOnly contract locally.
        if not isinstance(item, dict):
            raise TMDBError('TMDB returned an invalid catalogue item')
        if item.get('adult') is True:
            return False
        released = item.get('release_date')
        if not isinstance(released, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', released):
            return False
        try:
            return date.fromisoformat(released) <= today
        except ValueError:
            return False

    def catalog(self, kind, ident, offset=0):
        """Fetch one TMDB page, keeping its original cursor even after slicing.

        Completion is authoritative only for a validated TMDB response. Missing
        pages, errors, malformed previews and failed identity lookups all raise;
        they must not be mistaken for an empty catalogue by the caller.
        """
        definition = self._catalogs.get((kind, ident))
        if definition is None:
            raise ValueError('Unknown direct TMDB catalogue')
        if not _integer(offset) or offset >= self.PAGE_SIZE * self.MAX_PAGE:
            raise ValueError('TMDB catalogue offset is outside the supported range')
        page = offset // self.PAGE_SIZE + 1
        media = 'movie' if kind == 'movie' else 'tv'
        endpoint = ('/movie/now_playing' if definition.get('filters', {}).get('listType') == 'now_playing'
                    else '/discover/' + media)
        response = self._fetch(endpoint, self._parameters(definition, page))
        items = response.get('results')
        total_pages, total_results = response.get('total_pages'), response.get('total_results')
        if (not isinstance(items, list) or len(items) > self.PAGE_SIZE
                or not _integer(response.get('page'), 1) or response['page'] != page
                or not _integer(total_pages) or not _integer(total_results)):
            raise TMDBError('TMDB returned invalid catalogue pagination')
        expected_pages = (total_results + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        valid_pages = ({0, 1} if not total_results else {expected_pages})
        if expected_pages > self.MAX_PAGE:
            # The API limits accessible pages to 500. Its count may advertise
            # the full result set or that access cap; neither makes an early
            # short page authoritative.
            valid_pages.add(self.MAX_PAGE)
        expected_count = min(self.PAGE_SIZE, max(0, total_results - (page - 1) * self.PAGE_SIZE))
        if total_pages not in valid_pages or len(items) != expected_count:
            raise TMDBError('TMDB returned incomplete catalogue pagination')
        complete = page >= min(total_pages, self.MAX_PAGE)
        selected = items[offset % self.PAGE_SIZE:]
        if definition.get('filters', {}).get('listType') == 'now_playing':
            today = date.fromisoformat(self._today())
            selected = [item for item in selected if self._released_cinema_item(item, today)]
        previews = [self._preview(kind, item) for item in selected]
        # Filtering must never change the upstream cursor or completion signal:
        # a page containing only future releases is not an empty source page.
        return {'metas': previews, 'complete': complete,
                'next_offset': max(offset, (page - 1) * self.PAGE_SIZE + len(items))}
