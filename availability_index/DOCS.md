# Cached Media Library

Publishes catalogues and search results containing only titles whose stream was
recently verified as playable under your AIOStreams profile's own filters.

The app never proxies video, never stores stream URLs, and never writes to your
AIOStreams configuration.

## Configuration

| Option | Description |
| --- | --- |
| `aiostreams_url` | Base URL of the AIOStreams instance, for example `http://192.168.0.13:3000`. |
| `active_uuid` | UUID of the AIOStreams profile to mirror. Used read-only to verify the cache filter. |
| `active_password` | Password of that profile. |
| `stremio_uuid` | UUID used in the profile's Stremio endpoint. Usually the same as `active_uuid`. |
| `stremio_encrypted_password` | The profile's encrypted password, the last segment of its Stremio URL. |
| `catalog_uuid` | Optional. UUID of the profile whose catalogues are indexed. Defaults to `stremio_uuid`. |
| `catalog_encrypted_password` | Optional. Encrypted password of that profile. |
| `endpoint_token` | Private token protecting this app's Stremio endpoint. Generated and remembered when left empty. |
| `positive_hours` | How long a confirmed cached stream stays published (1–48). |
| `negative_hours` | How long a negative result is trusted before rechecking (1–168). |
| `max_candidates_per_category` | How deep each catalogue is indexed (20–5000). |
| `max_episodes_per_series` | Episode checks queued per show (1–100), newest seasons first. |
| `metadata_workers` | Concurrent episode-list fetches (1–6). Episode lists for long-running shows are large. |
| `check_delay_seconds` | Pause each check worker takes between checks (0–10). |
| `workers` | Concurrent availability checks (1–6). |
| `scanner_instance_id` | AIOStreams `instanceId` of this app's preset. Used to avoid indexing itself. |

### Finding the Stremio credentials

AIOStreams exposes each saved configuration at
`http://HOST:3000/stremio/<stremio_uuid>/<stremio_encrypted_password>/manifest.json`.
Both values are on the configuration page; `stremio_uuid` is normally the same
UUID as `active_uuid`.

## Switch-over

The app publishes its own catalogues. Clients keep using your AIOStreams
Jellyfin-compatible endpoint; only the catalogue source changes.

1. Start the app and open its diagnostics at `http://HOST:8097/status`. Wait until
   `ready` is `true` and at least a few titles are verified.
2. In AIOStreams add a **custom** add-on with
   `instanceId` `cachedlibrary` (matching `scanner_instance_id`) and manifest URL
   `http://HOST:8097/<endpoint_token>/manifest.json`, with resources
   `catalog` and `meta`.
3. Disable the upstream catalogues in the profile's catalogue settings so they do
   not appear alongside the filtered copies. Disabled catalogues remain fetchable
   through the Stremio endpoint, so indexing continues.

Rollback: remove the custom add-on and re-enable the upstream catalogues. Nothing
in the app is one-way.

## Why disabling upstream catalogues does not stop indexing

AIOStreams hides disabled catalogues from clients and from client search, but its
Stremio endpoint still serves them by id. The app also remembers the catalogue list
in its database, so it keeps verifying new additions after the switch-over.

### Revising the shelves

Shelves come from the catalogue source profile, so that is the only place to edit
them. Add, remove, rename or reorder catalogues there and the app reconciles on its
next synchronisation: new shelves are crawled, removed shelves are retired along
with their titles and checks, and client order follows that profile's order.

AIOStreams truncates the list to `jellyfin.maxLibraries` (default 24), silently
dropping the last shelves. Raise it above the number of shelves you publish.

### Reinstalling or starting fresh

The app needs to learn which catalogues to index. After switch-over the family
catalogue list is served by this app itself, so the family manifest no longer
contains the upstream definitions. To stay reinstall-proof, point
`catalog_uuid`/`catalog_encrypted_password` at a profile that still has the upstream
catalogues enabled and is never switched over. A fresh install then bootstraps from
that profile instead of depending on data it no longer has. If those options are
empty the app falls back to the active profile's own manifest.

A catalogue that finishes crawling with no titles at all is not published, so a
stale definition cannot leave a permanently empty shelf.

## Operating notes

- **Indexing is progressive.** Sections fill as titles are verified; nothing waits
  for a complete crawl. A full first pass over the default depth takes hours.
- **Search covers what is indexed.** A cached title that has not been indexed yet
  will not appear in search. Raise `max_candidates_per_category` for broader cover.
- **Rate limits.** AIOStreams' `stremioStream` limiter defaults to 10 requests per
  15 seconds per IP. The default two workers stay under it and sustain roughly
  0.4–0.5 checks per second. Raising `workers` past 2 can trigger HTTP 429; the app
  backs off and retries rather than recording a false negative.
- **Series cost more than films.** Each show queues up to
  `max_episodes_per_series` checks, prioritising each newest season's opener.
- **Runtime state** is stored in the app's `/data` volume
  (`availability.sqlite`) and is included in Home Assistant backups.

## Monitoring

| Endpoint | Purpose |
| --- | --- |
| `/health` | `200` when ready and all workers are recent; `503` otherwise. Used by the image's `HEALTHCHECK`. |
| `/status` | Readiness, issue text, per-category verified/candidate counts, backlog and worker ages. |

Both are unauthenticated and deliberately expose no credentials, URLs or tokens.

## Troubleshooting

**`ready` is false with `AIOStreams is not excluding uncached streams` or
`AIOStreams is not requiring a cached TorBox stream`.** The mirrored profile no
longer enforces cached-only playback. Fix the profile; the app resumes publishing
automatically.

**`ready` is false with `aiostreams_url must be an http(s) URL`, `active_uuid and
active_password are required`, `stremio_uuid and stremio_encrypted_password are
required`, or `endpoint_token must be at least 24 characters when set`.** The app is
running but not indexed because its options are incomplete. Open the app
configuration, fill in the reported values, and restart. Nothing is lost: the app
reports the problem instead of crash-looping, and `/status` repeats it.

**`ready` is false with `no upstream catalogues found in the profile manifest`.**
The catalogue source profile has no browsable catalogues. Check
`catalog_uuid`/`catalog_encrypted_password`, or re-enable at least one catalogue in
the profile the app reads.

**`Synchronisation failed (HTTPError ...)`.** Check `aiostreams_url`, the profile
UUID/password pair and the Stremio credentials. A `404` usually means a wrong
`stremio_uuid` or encrypted password; `401` means a wrong profile password.

**Shelves look nearly empty straight after install.** The index verifies one
candidate at a time, so coverage grows for hours. `/status` shows how far it has
got: `verified_now` against `pending` plus `metadata_pending`. Nothing is being
wrongly dropped — check a title directly against the profile's `/stream` endpoint
to confirm the app agrees with AIOStreams. Series fill in last, because their
episode checks only exist once the (sometimes very large) episode list has been
fetched; `metadata_workers` and `check_delay_seconds` are the throughput levers.

**Sections are empty.** Nothing has been verified yet, or every candidate really
is uncached. Compare `/status` counts with the same title checked directly in
AIOStreams. A title with only uncached sources is working as intended: it is
hidden until a cached copy appears.

**A title is missing from search.** It has not been indexed yet, or it has no
cached stream. Verify it directly against the profile's `/stream` endpoint.

**The app reports unhealthy.** `/health` answers `503` until the profile has been
read successfully and all workers are recent, so a freshly started app is briefly
unhealthy by design. If it stays unhealthy, check the log for a synchronisation
failure and confirm the AIOStreams options.

**Do not add a `watchdog` key to `config.yaml`.** The Supervisor `watchdog` option
is obsolete; the Home Assistant add-on linter rejects it, and a boolean value makes
the Supervisor drop the app from the store. Container health is reported by the
image `HEALTHCHECK`, which polls `/health`.
