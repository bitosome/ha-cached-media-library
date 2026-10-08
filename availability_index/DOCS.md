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
| `max_episodes_per_series` | Progressive episode batch size, 1–100; default 12. All aired candidates remain eligible; this does not permanently cap visible episodes. |
| `workers` | Concurrent availability workers, 1–6; default 2. They share the rate budget below. |
| `metadata_workers` | Concurrent metadata fetches, 1–6; default 3. Long series can have large episode lists. |
| `stream_requests_per_minute` | Shared stream-check request budget across all workers, 1–600; default 30. Leave headroom for playback and other clients under AIOStreams' own limit. |
| `check_delay_seconds` | Additional pause after each worker's check, 0–10 seconds; default 1.5. Lowering it does not bypass shared pacing. |
| `recovery_snapshot` | Leave empty normally. Optional administrator-supplied snapshot for one-time recovery of still-valid confirmations; see recovery below. |
| `scanner_instance_id` | This app's custom-preset `instanceId` in AIOStreams; default `cachedlibrary`. Used to identify and avoid its own resources. |

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
