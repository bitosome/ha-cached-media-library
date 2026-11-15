"""Title, season and episode metadata read directly from TMDB's official API.

This replaces a hosted TMDB metadata add-on in the source profile. It only
requests metadata from api.themoviedb.org and never requests streams. Episode
ids keep the `<series id>:<season>:<episode>` form the stream checks already
use, so existing confirmations stay attached to their episodes.
"""
import json
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TMDBError(RuntimeError):
    """A credential-free, safe-to-log failure; never includes remote bodies."""


class TMDBNotFound(TMDBError):
    """TMDB authoritatively has no such title; retrying soon will not help."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a Bearer token to another host.
        return None


IMAGE = re.compile(r'/[A-Za-z0-9_-]+\.(?:jpg|png|webp|svg)')
DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
IMDB = re.compile(r'tt\d{7,12}')
SEASONS_PER_REQUEST = 20  # TMDB's append_to_response limit.
MAX_SEASONS = 400
MAX_EPISODES = 20000


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _image(path, size):
    if isinstance(path, str) and IMAGE.fullmatch(path):
        return 'https://image.tmdb.org/t/p/' + size + path
    return None


def _runtime(minutes):
    if not _integer(minutes, 1) or minutes > 6000:
        return None
    hours, rest = divmod(minutes, 60)
    return (str(hours) + 'h' if hours else '') + str(rest) + 'min'


def _rating(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 10:
        return None
    return str(round(float(value), 1))


def _names(items, limit=10):
    result = []
    for item in items if isinstance(items, list) else []:
        name = _text(item.get('name')) if isinstance(item, dict) else ''
        if name and name not in result:
            result.append(name)
    return result[:limit]


class TMDBClient:
    """Paced, read-only transport for fixed api.themoviedb.org paths."""

    def __init__(self, api_key, request_json=None, interval=0.2):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError('A TMDB API key or Read Access Token is required')
        self._credential = api_key.strip()
        self._opener = build_opener(_NoRedirect())
        self._pace_lock = threading.Lock()
        self._next_request = 0.0
        self._interval = interval
        self._request_json = request_json or self._http_json

    def _http_json(self, path, params):
        # Only internal fixed paths reach this method; no host is configurable.
        headers = {'Accept': 'application/json', 'User-Agent': 'CachedMediaLibrary/TMDB'}
        query = dict(params)
        if re.fullmatch(r'[a-fA-F0-9]{32}', self._credential):
            # v3 keys use query authentication. The URL stays inside this
            # transport and is never repeated in an error.
            query['api_key'] = self._credential
        else:
            headers['Authorization'] = 'Bearer ' + self._credential
        request = Request('https://api.themoviedb.org/3' + path + '?' + urlencode(query), headers=headers)
        with self._pace_lock:
            wait = self._next_request - time.monotonic()
            self._next_request = max(self._next_request, time.monotonic()) + self._interval
        if wait > 0:
            time.sleep(wait)
        try:
            with self._opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                raise TMDBNotFound('TMDB has no such title') from None
            raise TMDBError('TMDB request failed (HTTP ' + str(exc.code) + ')') from None
        except Exception:
            raise TMDBError('TMDB request failed') from None

    def fetch(self, path, params):
        try:
            result = self._request_json(path, params)
        except TMDBError:
            raise
        except Exception:
            raise TMDBError('TMDB request failed') from None
        if not isinstance(result, dict):
            raise TMDBError('TMDB returned an invalid response')
        if result.get('status_code') == 34:
            raise TMDBNotFound('TMDB has no such title')
        if result.get('success') is False or result.get('status_code'):
            raise TMDBError('TMDB returned an invalid response')
        return result


class TMDBMetadata:
    def __init__(self, fetch, language='en-US'):
        if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2}(?:-[A-Z]{2})?', language):
            raise ValueError('Invalid TMDB metadata language')
        self._fetch = fetch
        self.language = language

    def find(self, kind, imdb_id):
        """Resolve an IMDb id to the TMDB id of the same kind of title."""
        if kind not in ('movie', 'series') or not isinstance(imdb_id, str) or not IMDB.fullmatch(imdb_id):
            raise ValueError('Invalid IMDb identity lookup')
        result = self._fetch('/find/' + imdb_id, {'external_source': 'imdb_id'})
        items = result.get('movie_results' if kind == 'movie' else 'tv_results')
        if not isinstance(items, list):
            raise TMDBError('TMDB returned an invalid identity lookup')
        ids = [item['id'] for item in items if isinstance(item, dict) and _integer(item.get('id'), 1)]
        if not ids:
            raise TMDBNotFound('TMDB has no title for this IMDb id')
        return ids[0]

    def _details(self, kind, tmdb_id):
        media = 'movie' if kind == 'movie' else 'tv'
        extra = 'release_dates' if kind == 'movie' else 'content_ratings'
        result = self._fetch('/' + media + '/' + str(tmdb_id), {
            'language': self.language,
            'append_to_response': 'external_ids,credits,images,' + extra,
            'include_image_language': self.language[:2] + ',en,null'})
        if not _integer(result.get('id'), 1) or result['id'] != tmdb_id:
            raise TMDBError('TMDB returned details for another title')
        return result

    @staticmethod
    def _imdb(details):
        external = details.get('external_ids')
        for value in (external.get('imdb_id') if isinstance(external, dict) else None, details.get('imdb_id')):
            if isinstance(value, str) and IMDB.fullmatch(value):
                return value
        return None

    @staticmethod
    def _age_rating(kind, details):
        if kind == 'movie':
            block = details.get('release_dates')
            for entry in block.get('results', []) if isinstance(block, dict) else []:
                if isinstance(entry, dict) and entry.get('iso_3166_1') == 'US':
                    for release in entry.get('release_dates') or []:
                        value = _text(release.get('certification')) if isinstance(release, dict) else ''
                        if value:
                            return value
            return None
        block = details.get('content_ratings')
        for entry in block.get('results', []) if isinstance(block, dict) else []:
            if isinstance(entry, dict) and entry.get('iso_3166_1') == 'US' and _text(entry.get('rating')):
                return _text(entry['rating'])
        return None

    def _base(self, kind, ident, tmdb_id, details):
        name = _text(details.get('title' if kind == 'movie' else 'name'))
        if not name:
            raise TMDBError('TMDB returned a title without a name')
        meta = {'id': ident, 'type': kind, 'name': name, 'tmdb_id': str(tmdb_id),
                'description': _text(details.get('overview')), 'posterShape': 'poster'}
        original = _text(details.get('original_title' if kind == 'movie' else 'original_name'))
        if original:
            meta['original_title' if kind == 'movie' else 'original_name'] = original
        imdb = self._imdb(details)
        if imdb:
            meta['imdb_id'] = imdb
        genres = _names(details.get('genres'), 20)
        meta['genres'] = genres
        meta['genre'] = genres
        for source, target, size in (('poster_path', 'poster', 'w500'), ('backdrop_path', 'background', 'w1280')):
            url = _image(details.get(source), size)
            if url:
                meta[target] = url
        images = details.get('images')
        for logo in images.get('logos', []) if isinstance(images, dict) else []:
            url = _image(logo.get('file_path'), 'original') if isinstance(logo, dict) else None
            if url:
                meta['logo'] = url
                break
        rating = _rating(details.get('vote_average'))
        if rating:
            meta['imdbRating'] = rating
        age = self._age_rating(kind, details)
        if age:
            meta['ageRating'] = age
        countries = _names(details.get('production_countries'), 5)
        if countries:
            meta['country'] = ', '.join(countries)
        status = _text(details.get('status'))
        if status:
            meta['status'] = status
        credits = details.get('credits')
        if isinstance(credits, dict):
            cast = _names(credits.get('cast'), 10)
            if cast:
                meta['cast'] = cast
            crew = credits.get('crew') if isinstance(credits.get('crew'), list) else []
            for job, target in ((('Director',), 'director'), (('Writer', 'Screenplay', 'Story'), 'writer')):
                people = _names([p for p in crew if isinstance(p, dict) and p.get('job') in job], 5)
                if people:
                    meta[target] = people
        return meta

    def movie(self, ident, tmdb_id):
        details = self._details('movie', tmdb_id)
        meta = self._base('movie', ident, tmdb_id, details)
        released = details.get('release_date')
        if isinstance(released, str) and DATE.fullmatch(released):
            meta['released'] = released + 'T00:00:00.000Z'
            meta['releaseInfo'] = released[:4]
            meta['year'] = released[:4]
        runtime = _runtime(details.get('runtime'))
        if runtime:
            meta['runtime'] = runtime
        meta['behaviorHints'] = {'defaultVideoId': None, 'hasScheduledVideos': False}
        return meta

    def series(self, ident, tmdb_id):
        details = self._details('series', tmdb_id)
        meta = self._base('series', ident, tmdb_id, details)
        creators = _names(details.get('created_by'), 5)
        if creators and 'writer' not in meta:
            meta['writer'] = creators
        first, last = details.get('first_air_date'), details.get('last_air_date')
        if isinstance(first, str) and DATE.fullmatch(first):
            meta['released'] = first + 'T00:00:00.000Z'
            ended = details.get('status') in ('Ended', 'Canceled') and isinstance(last, str) and DATE.fullmatch(last)
            span = first[:4] + '-' + (last[:4] if ended else '')
            meta['releaseInfo'] = span if span != first[:4] + '-' + first[:4] else first[:4]
            meta['year'] = meta['releaseInfo']
        runtimes = details.get('episode_run_time')
        runtime = _runtime(runtimes[0]) if isinstance(runtimes, list) and runtimes else None
        if runtime:
            meta['runtime'] = runtime
        seasons = details.get('seasons')
        if not isinstance(seasons, list) or len(seasons) > MAX_SEASONS:
            raise TMDBError('TMDB returned an invalid season list')
        numbers = []
        for season in seasons:
            if not isinstance(season, dict) or not _integer(season.get('season_number')):
                raise TMDBError('TMDB returned an invalid season')
            if season['season_number'] not in numbers:
                numbers.append(season['season_number'])
        videos = []
        for start in range(0, len(numbers), SEASONS_PER_REQUEST):
            chunk = numbers[start:start + SEASONS_PER_REQUEST]
            result = self._fetch('/tv/' + str(tmdb_id), {
                'language': self.language,
                'append_to_response': ','.join('season/' + str(n) for n in chunk)})
            if result.get('id') != tmdb_id:
                raise TMDBError('TMDB returned seasons for another title')
            for number in chunk:
                season = result.get('season/' + str(number))
                # A listed season missing from the response is a truncated
                # answer, not proof that its episodes disappeared.
                if not isinstance(season, dict) or not isinstance(season.get('episodes'), list):
                    raise TMDBError('TMDB returned an incomplete season')
                for episode in season['episodes']:
                    video = self._video(ident, number, episode, runtime)
                    if video:
                        videos.append(video)
                if len(videos) > MAX_EPISODES:
                    raise TMDBError('TMDB returned too many episodes')
        meta['videos'] = videos
        meta['behaviorHints'] = {'defaultVideoId': None, 'hasScheduledVideos': True}
        return meta

    @staticmethod
    def _video(ident, season, episode, series_runtime):
        if not isinstance(episode, dict) or not _integer(episode.get('episode_number'), 1):
            return None
        number = episode['episode_number']
        video = {'id': ident + ':' + str(season) + ':' + str(number), 'season': season,
                 'episode': number, 'number': number,
                 'name': _text(episode.get('name')) or 'Episode ' + str(number)}
        overview = _text(episode.get('overview'))
        if overview:
            video['overview'] = overview
            video['description'] = overview
        aired = episode.get('air_date')
        if isinstance(aired, str) and DATE.fullmatch(aired):
            # Episodes without an air date stay unreleased for the index.
            video['released'] = aired + 'T00:00:00.000Z'
            video['firstAired'] = video['released']
        thumbnail = _image(episode.get('still_path'), 'w500')
        if thumbnail:
            video['thumbnail'] = thumbnail
        rating = _rating(episode.get('vote_average'))
        if rating:
            video['rating'] = rating
        runtime = _runtime(episode.get('runtime')) or series_runtime
        if runtime:
            video['runtime'] = runtime
        return video

    def meta(self, kind, ident, tmdb_id=None):
        """Build Stremio metadata for an indexed `tt…` or `tmdb:<n>` title.

        `tmdb_id` is a hint from discovery. It is used only when TMDB confirms
        that it belongs to the requested IMDb id; otherwise the id is resolved.
        Returns (meta, tmdb_id).
        """
        if kind not in ('movie', 'series') or not isinstance(ident, str):
            raise ValueError('Invalid metadata request')
        build = self.movie if kind == 'movie' else self.series
        native = re.fullmatch(r'tmdb:([1-9]\d{0,17})', ident)
        if native:
            number = int(native.group(1))
            return build(ident, number), number
        if not IMDB.fullmatch(ident):
            raise TMDBNotFound('Not a TMDB or IMDb identity')
        if _integer(tmdb_id, 1):
            try:
                meta = build(ident, tmdb_id)
            except TMDBNotFound:
                meta = None
            if meta is not None and meta.get('imdb_id') == ident:
                return meta, tmdb_id
        number = self.find(kind, ident)
        meta = build(ident, number)
        meta['imdb_id'] = ident
        return meta, number
