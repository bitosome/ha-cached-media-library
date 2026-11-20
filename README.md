# Cached Media Library

Home Assistant app that gives an [AIOStreams](https://github.com/Viren070/AIOStreams)
Jellyfin-compatible server discovery shelves and search containing titles with
**recently confirmed cached streams**.

The app checks candidates in the background through your existing AIOStreams
filters. It publishes confirmed movies and series; a series is confirmed by its
newest season's opener (or its first season's as a fallback) and then lists every
aired episode.
Availability can change after a check; normal cached-only playback filtering still
runs when you choose a stream.

[![Open your Home Assistant instance and add this repository.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fbitosome%2Fha-cached-media-library)

## The idea

Streaming through a debrid service means a title is only worth showing when at
least one release of it is *already cached* on the service. Everything else is a
dead click. This app answers one question continuously — *"would this title
actually play right now?"* — and removes everything else from view:

- **Shelves** (Popular, New, Top Rated, genre shelves, curated lists) contain only
  titles confirmed playable.
- **Search** runs on demand: results are discovered, checked for cached streams,
  and only playable ones are returned.
- For **series**, confirmation is per title (since 0.11.0): one cached season
  opener puts the show on the shelf, and every aired episode is listed. Debrid
  caches hold whole torrents, usually season packs, so an opener stands for its
  season; the playback profile still decides on each episode when it is played.
- Language preferences (for example Russian-first) are **sorting, not filtering**:
  a Russian release sorts to the top when one exists, and cached releases in any
  other language are still returned when it does not.

Playback stays between the client and the debrid provider's CDN. The app never
stores stream URLs, never proxies video, and never modifies AIOStreams.

## Architecture: why two AIOStreams profiles

The app talks to **two separate AIOStreams profiles**. They have different jobs,
and conflating them breaks the system:

| | Playback profile | Source profile |
| --- | --- | --- |
| Used for | Stream verdicts ("/stream" checks) and policy fingerprint | Shelf contents, metadata, discovery |
| Filters | The real family policy: cached-only, quality exclusions, title matching | Unfiltered: original catalogues and metadata providers |
| Sees this app? | Yes — publishes its shelves | **Never** — it is the raw input |

- **The playback profile is the verdict authority.** A title counts as playable
  only if it has streams *under the same filters the client will use*. Anything
  looser would mark titles "available" that then fail to play.
- **Discovery must be unfiltered.** A filtered source could never report titles
  that are not yet verified — and a title must be known before it can be
  verified. Chicken and egg.
- **Circularity:** the playback profile's manifest includes this app's own
  shelves. Reading discovery from there would mean indexing ourselves. The app
  refuses to start with such a configuration.

Each AIOStreams profile is its own UUID+password pair; there is no
shared-credential mode. The app never needs administrator credentials.

## How it works

1. **Policy sync.** Every few minutes the app reads the playback profile and
   confirms cached-only filtering is still enforced (`excludeUncached` plus a
   required `cached(service(streams, ...))` expression). It fingerprints every
   setting that can change stream acceptance; a fingerprint change invalidates
   all stored verdicts and re-checks progressively. Presentation-only settings
   (formatter, sorting, branding) never invalidate.
2. **Discovery.** Local TMDB shelf definitions are crawled directly from the
   official TMDB API; other shelves come from the source profile. Title, season
   and episode metadata likewise comes straight from TMDB when an API key is set.
3. **Verification.** Candidates are checked against the playback profile's
   `/stream` endpoint. Provider failures, informational cards and ambiguous
   empty responses are retried as *unknown* — only an explicit filtering report
   proves absence, and a failure never shortens an earlier positive confirmation.
4. **Ordering.** A series has at most two checks: its newest aired season's
   first episode, then its first season's first episode, which runs only while
   the newest one is not confirmed. Specials are probed only when a show has
   nothing else. Every client visit — a search, opening a title — prioritises
   that title's pending checks.
5. **Expiry and pacing.** Confirmations expire (`positive_hours` /
   `negative_hours`) and are renewed before they lapse. Workers share a global
   request rate and back off together when an upstream provider rate-limits.

## Shelves

Shelves come from local TMDB definitions (the `tmdb_catalogs` option) and from
the source profile's catalogues. The app reconciles them on every sync: renamed
or reordered shelves follow the source; removed shelves are retired; re-enabled
shelves are re-crawled.

A local definition looks like this — a sci-fi movie shelf sorted by popularity,
limited to settled, widely voted releases:

```json
{
  "id": "native-tmdb-scifi-movies",
  "type": "movie",
  "name": "Sci-Fi Movies",
  "filters": {
    "listType": "discover",
    "genres": [878],
    "sortBy": "popularity.desc",
    "voteCountMin": 300,
    "includeAdult": false,
    "releasedOnly": true
  }
}
```

Useful filters include `genres` / `excludeGenres`, `voteCountMin`,
`ratingMin`/`ratingMax`, `releasedWithinDays` / `releasedBeforeDays`,
`originalLanguages` / `excludeOriginalLanguages`, `keywords` /
`excludeKeywords`, `companies`, `networks` (series), `tvType` (series),
`certificationCountry` with exact `certifications` (use these for children's
shelves) or `certificationMax` (movies; it also matches unrated titles)
(movies and series, e.g. `["TV-Y"]`), `airedWithinDays` (series with a recent
episode), `region` and
`listType: "now_playing"` (movies). TMDB has no separate sci-fi/fantasy genres
for series — 10765 is the combined "Sci-Fi & Fantasy".

Notes:

- TMDB's discover API caps browsing at 500 pages, so very broad shelves truncate
  at `max_candidates_per_category`.
- Retire a shelf by keeping its definition with `"retired": true`, not by
  deleting it: a deleted local id falls back to being read from the source
  profile under the same identity. Retiring removes confirmations of titles that
  are on no other shelf.
- Bump `catalog_revision` after editing definitions to re-crawl shelves.
- Shelves are displayed newest first (`shelf_order: newest`, since 0.12.0):
  the definition picks which titles are on a shelf (e.g. the 300 most popular),
  then they are shown by release date — series by their latest aired episode.
  Sorting a definition by release date instead would lead with obscure titles.
- Set AIOStreams `jellyfin.maxLibraries` high enough for the published shelf
  count.

## Search and languages

Search covers indexed candidates plus on-demand TMDB discovery; only confirmed
titles are returned. Title matching is language-robust:

- All known aliases (original, English, translated names) are stored per title,
  so a title found once stays findable in any of its languages.
- A query containing Cyrillic automatically asks TMDB for Russian metadata while
  retaining the original title.
- Matching is conservative by design: words may be reordered and articles
  omitted ("Puss in Boots" finds "The Puss in Boots"), the last word may be a
  prefix, and sequel numbers never match a different entry ("Toy Story 2" will
  not match "Toy Story 20").

## Configuration

All options live in the app's Supervisor configuration. The essentials:

| Option | Default | Meaning |
| --- | --- | --- |
| `aiostreams_url` | — | Base URL of the AIOStreams app |
| `active_uuid` / `active_password` | — | Playback profile credentials (policy + verdicts) |
| `stremio_uuid` / `stremio_encrypted_password` | — | Playback profile's Stremio endpoint credentials |
| `catalog_uuid` / `catalog_encrypted_password` | — | **Source** profile's Stremio endpoint credentials (must differ from the playback profile) |
| `endpoint_token` | generated | Token in this app's own manifest URL; ≥24 chars when set by hand |
| `positive_hours` / `negative_hours` | 12 / 24 | How long playable / not-playable verdicts are trusted |
| `workers` | 2 (max 6) | Parallel availability checkers |
| `metadata_workers` | 3 (max 6) | Parallel metadata fetchers |
| `stream_requests_per_minute` | 30 (max 600) | Global pacing across all workers |
| `check_delay_seconds` | 1.5 | Extra per-worker delay between checks |
| `max_candidates_per_category` | 250 | Crawl depth per shelf |
| `catalog_refresh_hours` | 6 | Shelf re-crawl interval |
| `max_episodes_per_series` | 12 | Worker's per-visit batch size (never a permanent cap) |
| `tmdb_api_key` / `tmdb_language` | — / `en-US` | Enables direct TMDB discovery and metadata |
| `tmdb_catalogs` | `[]` | Local shelf definitions (see above) |
| `recovery_snapshot` | — | One-shot restore of previously exported confirmations |

Throughput rule of thumb: actual checks per second ≈
`min(stream_requests_per_minute/60, workers / (request time + check_delay_seconds))`.
At 6 workers and 240 rpm a full library rebuild of ~190k checks drains in
roughly half a day; steady-state renewals need far less.

## Monitoring and troubleshooting

`GET /status` (no token required, contains no credentials) reports readiness,
per-shelf candidate/verified counts, queue depth (`pending`), retrying checks
and worker health. `GET /status?lookup=<imdb id>` explains why one specific
title is or is not published. The manifest is served at
`/<endpoint_token>/manifest.json`.

Common symptoms:

- **A shelf is thin.** Those are pending checks, not missing content — the
  queue drains in priority order, and anything you search for jumps the queue.
  `pending` on `/status` tells you the backlog.
- **An episode of a listed show has no streams.** Possible since 0.11.0: shows
  are confirmed per title, so an episode outside a cached season pack can be
  listed without a cached file. The playback profile then offers nothing.
- **Everything went back to "pending" at once.** The playback profile's policy
  changed (any filter/provider/preset edit, or an AIOStreams upgrade that
  introduces new settings fields). That is deliberate: verdicts are only trusted
  under the policy that produced them.
- **`UNABLE_TO_VERIFY_LEAF_SIGNATURE` in the AIOStreams log.** A network filter
  (for example UniFi content filtering / SSL inspection) is intercepting a
  provider's domain and serving its own certificate. Allowlist the domain or
  exempt the Home Assistant host; verify with `curl -sv https://<domain>/` and
  check the certificate issuer.
- **"Too Many Requests" from a provider.** Chronic upstream rate limiting; the
  shared cooldown backs off and retries automatically. Persistent abuse-level
  traffic just slows verification — it never corrupts verdicts.
- **`metadata.identity_conflict_titles` on `/status`.** Shows (mostly long
  anime) whose TMDB season numbering contradicts their existing checks. Their
  stored episodes are kept and they are re-examined daily; nothing plays worse
  because of this.

Backup: the whole state is `/data/availability.sqlite` inside the app — a normal
Home Assistant app backup covers it. Rollback after a bad upgrade is installing
the previous app version; confirmations survive downgrades.

## Install or upgrade

1. Add `https://github.com/bitosome/ha-cached-media-library` in Home Assistant's app
   store repository settings, then install **Cached Media Library**.
2. Configure both profiles and the app options using the
   [setup and switch-over instructions](availability_index/DOCS.md).
3. Wait for successful synchronisation and verified titles before enabling the
   filtered catalogues in the playback profile.

Version 0.7.0 requires the separate source profile even on existing installations.
On upgrade it schedules series metadata for a fresh fetch to recover full episode
lists. The first v0.7.0 synchronisation revalidates older verdicts under the expanded
policy fingerprint. Shelves refill progressively as titles and episodes are checked.

## Development

The app uses Python's standard library:

```sh
python3 -m unittest discover -s availability_index/tests -v
```

For a local Home Assistant app installation:

```sh
scp -O -r availability_index root@HOME_ASSISTANT:/addons/
ssh root@HOME_ASSISTANT 'ha store reload && ha apps update local_availability_index'
```

The Supervisor offers an update only when `config.yaml`'s version increases.
Back up the app before upgrading. Docker's health check reports failures; it is
not a worker restart mechanism by itself.

## Licence

MIT — see [LICENSE](LICENSE).
