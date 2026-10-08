# Cached Media Library

Publishes discovery shelves, search results and series episodes with recently
confirmed cached streams under your AIOStreams playback profile's filters. It
never proxies video, stores stream URLs, or writes AIOStreams configuration.

A confirmation is a recent observation, not a playback guarantee. A provider may
remove a cached file or become unavailable afterward. Keep cached-only filtering
enforced in the playback profile so a stale library entry cannot enable downloading
an uncached stream.

## Profiles: configure these before switch-over

Two separate AIOStreams profiles are required:

- **Playback profile:** the profile used by Infuse or another Jellyfin client.
  Its settings determine acceptable streams. The app reads its configuration with
  `active_uuid` and `active_password`, and checks streams through its
  `stremio_uuid` and `stremio_encrypted_password` endpoint. These must refer to the
  same playback profile.
- **Source profile:** keeps the original upstream catalogue and metadata providers
  enabled. Use its `catalog_uuid` and `catalog_encrypted_password` here. Its UUID
  must differ from the playback profile. Do not add Cached Media Library to this
  profile or hide its upstream catalogues.

The source profile supplies complete candidate lists and episode metadata. Reading
metadata back from the filtered playback profile would progressively restrict the
scanner to the episodes it had already published. The app rejects incomplete or
unsafe source configuration instead of relying on an old database to bootstrap.

Only the playback profile's ordinary configuration password is needed. Do not enter
AIOStreams dashboard administrator credentials. The app does not change either
profile or modify the dashboard's error-display settings.

### Finding the Stremio credentials

A saved profile has an endpoint of the form:

```text
http://HOST:3000/stremio/<uuid>/<encrypted_password>/manifest.json
```

Copy the two segments from each profile's configuration page. The encrypted
password is different from the password used to open that profile. Treat these
URLs as credentials; avoid pasting them into public logs or issue reports.

## Configuration

| Option | Description |
| --- | --- |
| `tmdb_api_key` | Optional TMDB v3 API key or Read Access Token for direct discovery. |
| `tmdb_language` | Metadata language for direct discovery; default `en-US`. |
| `tmdb_catalogs` | JSON array of local catalogue definitions; default `[]` disables direct discovery. |
| `aiostreams_url` | AIOStreams base URL, for example `http://192.168.0.13:3000`. |
| `active_uuid` | Playback profile UUID, used read-only to verify its filters. |
| `active_password` | Playback profile configuration password, not dashboard administrator password. |
| `stremio_uuid` | Playback profile's Stremio UUID; must match `active_uuid`. |
| `stremio_encrypted_password` | Playback profile's encrypted Stremio password. |
| `catalog_uuid` | Required separate source profile UUID, distinct from the playback profile. |
| `catalog_encrypted_password` | Required source profile's encrypted Stremio password. |
| `endpoint_token` | Private token protecting this app's metadata endpoint. Generated and remembered if empty; an explicitly set token must contain at least 24 characters. |
| `positive_hours` | Confirmation lifetime, 1–48 hours; default 12. Shorter lifetimes improve freshness but need more rechecks. |
| `negative_hours` | Lifetime of a confirmed negative result, 1–168 hours; default 24. This does not apply to provider errors or unexplained empty responses. |
| `max_candidates_per_category` | Upstream candidate depth per shelf, 20–5000; default 250. It is not a target number of verified results. |
| `catalog_refresh_hours` | Refresh interval for completed source catalogues, 1–48 hours; default 6. Availability workers run continuously regardless of this interval. |
| `catalog_revision` | Optional manual marker, empty by default. Change it after saving source selection filters to request one fresh crawl of every active shelf. Unchanged values do not restart crawling. |
| `max_episodes_per_series` | Progressive episode batch size, 1–100; default 12. All aired candidates remain eligible; this does not permanently cap visible episodes. |
| `workers` | Concurrent availability workers, 1–6; default 2. They share the rate budget below. |
| `metadata_workers` | Concurrent metadata fetches, 1–6; default 3. Long series can have large episode lists. |
| `stream_requests_per_minute` | Shared stream-check request budget across all workers, 1–600; default 30. Leave headroom for playback and other clients under AIOStreams' own limit. |
| `check_delay_seconds` | Additional pause after each worker's check, 0–10 seconds; default 1.5. Lowering it does not bypass shared pacing. |
| `recovery_snapshot` | Leave empty normally. Optional administrator-supplied snapshot for one-time recovery of still-valid confirmations; see recovery below. |
| `scanner_instance_id` | This app's custom-preset `instanceId` in AIOStreams; default `cachedlibrary`. Used to identify and avoid its own resources. |

## Direct TMDB discovery (0.8.0)

Discovery can run inside this app against the official `api.themoviedb.org` API.
No hosted TMDB Discover service is required. Set `tmdb_api_key` to your TMDB v3
key or API Read Access Token, `tmdb_language` to a metadata language such as
`en-US`, and `tmdb_catalogs` to a JSON array of catalogue definitions:

```json
[{"id":"local-best-movies","type":"movie","name":"Best Movies","filters":{"listType":"discover","sortBy":"vote_average.desc","ratingMin":7,"voteCountMin":500,"includeAdult":false,"releasedOnly":true,"releaseTypes":[1,2,3,4,5,6]}}]
```

Each definition has a stable `id`, `type` (`movie` or `series`), `name` and
`filters`. Selection supports genre IDs, excluded genres, rating/vote thresholds,
released-only date cutoffs, sorting and scripted/miniseries filters. For scripted series use
`tvType: "2|4"` and `excludeGenres: [10763,10764,10767]`. Genre `16` selects
animation; it is not an age rating. Language affects metadata, not the language
of available audio. Russian/English audio preferences remain in AIOStreams.

Keep catalogue IDs unchanged when editing filters. The app detects changes to
local definitions or metadata language and starts one new crawl while retaining
existing confirmations. Only candidates passing the normal AIOStreams checks
are published. Direct TMDB paging has an explicit completion signal, so a
successfully completed shorter or empty selection removes obsolete members.
Transport errors, rate limits and invalid responses retain membership for retry.

**Migrating an existing hosted discovery provider:** use each catalogue's exact
source-profile ID, including its AIOStreams prefix, and keep its original type.
This preserves the derived Infuse shelf IDs. Enable and verify direct discovery
before removing the hosted catalogue preset from the source profile. The app
prefers local definitions for those IDs. Existing explicit TMDB/IMDb mappings
seed a persistent cache; new mappings are resolved directly from TMDB. Mapping
cache entries do not establish stream availability or extend confirmations.

The separate source profile is still required for complete episode metadata and
any other catalogues, including curated family selections. Its metadata provider
must remain enabled; replacing discovery does not replace every AIOStreams
metadata or stream provider. Do not add the filtered cache app to that profile.

Leave `tmdb_catalogs` as `[]` to retain the previous source-profile-only mode.
The TMDB key stays in Home Assistant app options and is sent only to the official
TMDB API. It is not published in the manifest, status response or catalogue output.
TMDB requests are paced separately from TorBox availability checks. Positive ID
mappings last seven days, confirmed missing IMDb mappings one day. No media is
served through this app.

### Recent releases and cinema discovery (0.8.1)

Use `releasedWithinDays` (1–3650) in a discover definition to bound its release
window. For example, 180 days for new movies and 365 for new series. Movies use
their primary release date; series use their first premiere date, not the latest
episode or season. The UTC date boundaries advance automatically with each crawl.
`releasedOnly` must remain enabled with this option. Sorting by date alone does
not imply a recent-release window.

For a cinema shelf use a movie definition with filters such as:

```json
{"listType":"now_playing","region":"EE","includeAdult":false,"releasedOnly":true}
```

This calls TMDB’s official Now Playing endpoint, optionally using a two-letter
country code. Its calendar can include upcoming days, so the app excludes entries
with future, missing or invalid release dates and adult entries. Older films
listed as theatrical reissues remain eligible. Pagination always follows the raw
TMDB cursor, even when a whole page is excluded. Unsupported extra filters are
rejected; ratings, genre and sort filters cannot silently alter this mode.

A cinema feed is discovery metadata, not live local showtimes. Only titles with
acceptable cached streams are published, so this section can legitimately be
short or empty. Cinema discovery never relaxes the playback policy.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Installation and switch-over

1. Back up the existing AIOStreams configuration. Create the separate source profile
   described above and verify that its original catalogue and metadata providers
   work before hiding anything in the playback profile.
2. In the playback profile, enable `excludeUncached` and the required expression
   `cached(service(streams, 'torbox'))`. Keep the intended quality and language
   filters enabled. Configure both profiles' credentials in this app and start it.
3. Open the app's Home Assistant panel or `http://HOST:8097/status`. Wait for `ready`
   to become `true` and for useful titles to be verified. Initial coverage grows
   progressively and can take hours.
4. Add the app to the **playback profile only** as a custom add-on, using the
   `instanceId` configured above and the manifest URL shown in the app's startup
   log: `http://HOST:8097/<endpoint_token>/manifest.json`. Enable `catalog` and
   `meta` resources. Keep the playback profile's stream providers enabled.
5. Disable the original discovery and search catalogues in the playback profile so
   they cannot bypass the filtered library. Ensure the app's metadata is used for
   client-facing series lists. Leave the source profile unchanged.
6. Set short AIOStreams cache lifetimes for this app's resources: **30 seconds for
   catalogue and metadata responses, 60 seconds for its manifest**. Verify the
   effective AIOStreams cache settings; HTTP headers or Stremio hints alone may not
   override AIOStreams' configured cache. Longer caching can keep expired entries
   or old shelf definitions in clients after the index changes.
7. Set `jellyfin.maxLibraries` high enough for all shelves, then refresh the library
   in Infuse. Check both browsing and search, including a series' episode list.

Rollback: remove or disable the custom add-on in the playback profile and restore
its original catalogue and metadata settings. The source profile and stored index
are unchanged by that operation.

## Upgrading to 0.7.0

Take a Home Assistant app backup first. Existing installations now need a distinct
source profile with working catalogue and metadata providers; configure its two
credentials before switching over or restarting the upgraded app.

The upgrade schedules series metadata to be fetched again from that source,
recovering full episode lists. The expanded policy fingerprint revalidates old
verdicts during the first synchronisation. Shelves refill progressively, and later
restarts retain confirmations until normal expiry or another policy change.
Additional aired episodes are checked rather than permanently excluded by a cap.

If previously truncated series still look unchanged, compare the app's per-title
diagnostics with the source profile, allow time for checking, and confirm the
AIOStreams and client metadata caches have refreshed.

### Recovering confirmations invalidated by the 0.7.0 migration

Version 0.7.2 can restore still-valid confirmations from a trusted pre-upgrade
backup **only after verifying that its effective playback policy is unchanged**.
This is an administrator repair, not a way to mark unchecked titles as available.
The optional `recovery_snapshot` Home Assistant setting carries a base64url-encoded,
zlib-compressed JSON object with `version: 1`, the current `policy_fingerprint`
value in `policy`, and `checks` containing `type`, `id`, `parent`, `checked`,
`expires`, `due`, and `count` copied from positive backup records. Never generate
this snapshot from an unrelated profile or merely replace its policy hash.

The importer repairs only pending records belonging to active titles. It retains
original check times and expiry (shortening expiry if the configured positive
lifetime is now lower), does not overwrite newer conclusive observations, and
records the snapshot digest atomically to prevent replay. Check `/status`'s
`recovery` result, then clear `recovery_snapshot`. No public write API is exposed.
Normal restarts and 0.7.1-to-0.7.2 upgrades retain existing confirmations.

## Shelves, genres and search

After changing filters at an upstream catalogue provider, save those provider
settings first, then set `catalog_revision` to a new marker such as
`2026-10-08-selection-v1` and restart the app to load its updated options. The next
synchronisation starts a new crawl for every active shelf, including curated lists.
It retains current members and their confirmations until a complete new crawl can
replace the shelf selection. The marker is manual: the app does not automatically
detect upstream filter edits, and ordinary synchronisation with the same marker
does not restart the crawl. Leaving it empty on upgrade does not add a migration
crawl to an already configured index.

This does not stage removal of catalogue IDs: removing a catalogue from the source
manifest still retires it immediately. When replacing whole feeds, keep the old
feeds enabled until their replacements have been crawled and verified, then remove
the old feeds separately.

Source catalogue crawling runs in the background. Increasing the configured depth
immediately schedules paginated shelves for a fresh crawl, preserving existing
memberships and availability confirmations while additional candidates are learned.
Version 0.7.3 re-crawls existing active shelves once to establish the saved depth;
ordinary synchronisation does not repeatedly restart a crawl.

An empty upstream response can also mean a provider failure. It does not by itself
remove existing members; the app retries after ten minutes if an empty response
would shrink the shelf. This can retain earlier members when a source genuinely
shrinks until a later complete, nonempty response confirms the change. Removing
the catalogue from the source profile still retires that shelf explicitly.

The source profile controls shelf names, order and candidate selection. Changes
are reconciled during synchronisation. Removed shelves are retired; re-enabled
shelves are crawled again. A completely empty upstream shelf is not published once
its crawl finishes. A shelf with candidates but no confirmed results may be empty
while checks are pending or when none of its candidates qualify.

Genre options are published where source data supplies genres. Titles without
genre metadata cannot be assigned reliably. Known IMDb/TMDB aliases are combined
where metadata establishes that they identify the same title. Episode evidence
is shared only between aliases with the same explicit IMDb identity and exact
episode request ID; timestamps and expiry are preserved, and newer negatives win.
This avoids losing confirmed episodes when the client sees a different alias.
Translated names
remain searchable across catalogue refreshes.

Opening or searching for a known title advances its pending checks, with a
five-minute cooldown per title. Unverified results stay hidden; retry the search
after the checks complete. This priority does not bypass the shared request limit.

**Search covers indexed candidates only.** A cached title outside those catalogues
or their configured depth will not appear. Raising catalogue depth improves
coverage at the cost of more checking. On-demand upstream discovery is not part
of this version.

The v0.7.0 policy fingerprint is intentionally broader. Its first synchronisation
invalidates verdicts from earlier versions, so the visible library temporarily
shrinks while confirmations are rebuilt. Version 0.7.2 supports the guarded
backup recovery described above. Later restarts do not repeat this reset.
Inherited or conditional playback profiles currently fail closed; use a directly
saved playback profile until effective-policy resolution is supported.

## Availability, retries and pacing

Movie and series work use separate queues and alternate when both have work.
Unverified shows receive one probe per rotation to spread coverage across titles;
verified or explicitly requested shows can use the configured episode batch.
Still-valid confirmations due for renewal take priority over expanding the
unchecked backlog. Expired confirmations rejoin ordinary scheduling. A large
complete-episode index can still take days to cover within provider limits.


- A movie needs a playable stream surviving the playback profile's filters. A
  series needs at least one confirmed aired episode; its published metadata
  contains only its confirmed episodes.
- A successful confirmation expires after `positive_hours`, with a refresh
  scheduled before expiry. Failed refreshes do not extend the confirmation.
- An explicit negative result is retained for `negative_hours` (24 by default).
  Provider timeouts, rate limits and unexplained empty responses remain unknown
  and are retried with backoff. They neither prove absence nor publish a title.
  When the playback profile hides provider errors, all no-stream results remain
  unknown: even a filtering notice could conceal another provider's failure.
- Acceptance-policy changes invalidate old results. Do not expect prior
  confirmations to survive stricter quality, language or source filters.
- All stream workers share `stream_requests_per_minute` and an upstream cooldown.
  Configure this below the effective AIOStreams limit, accounting for other users.
  More workers can overlap slow requests but do not increase the shared budget.
- The source and playback profiles may cache their own upstream responses. The
  observation's freshness therefore also depends on those provider caches.

Runtime state lives in `/data/availability.sqlite` and is included in Home
Assistant app backups. No stream URLs or video files are stored by this app.

## Monitoring and troubleshooting

| Endpoint | Purpose |
| --- | --- |
| `/health` | HTTP 200 when ready and worker heartbeats are recent; HTTP 503 otherwise. Used by Docker's health check. |
| `/status` | Readiness, issue text, category counts, backlog and worker ages. Also shown by the Home Assistant panel. |
| `/status?lookup=series:tt123` | Per-title metadata and availability diagnostics; replace the identifier with the title being investigated. Use `movie:` for films. |

These diagnostics are unauthenticated and contain no credentials or stream URLs.
Keep port 8097 on your trusted network. The Stremio endpoint itself requires the
private token. The startup log contains its URL, so redact it before sharing logs.

**Not ready after installation or upgrade:** read `issue` in `/status`. Check both
profiles' credentials, confirm they are distinct, and ensure the source profile
has upstream catalogues and metadata enabled. It must not include this app.

**Cached-only policy rejected:** restore both `excludeUncached` and the required
cached TorBox expression in the playback profile. The app resumes after successful
synchronisation; it does not repair the profile itself.

**Missing title or episode:** check `/status?lookup=...`, then compare the source
metadata and playback profile's stream response. A title may be outside indexed
coverage, pending a check, unknown after a provider failure, or confirmed absent.
Do not assume an empty search result proves the debrid provider has no cached copy.

**Russian search stopped finding a title:** verify that either its catalogue
preview or metadata actually contains the Russian name. Search preserves names
that source data provides; it cannot invent missing translations.

**Unhealthy app:** inspect the reported issue and worker ages. Docker's health
check reports failure but does not itself restart a dead worker. If a worker stays
stale, retain sanitized diagnostic information and restart the app through Home
Assistant; investigate recurring failures rather than relying on health status
alone.
