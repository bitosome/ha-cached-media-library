# Changelog

## 0.8.1

- Add rolling release windows for honest recent-movie and series-premiere categories.
- Add regional In Cinemas discovery using TMDB’s official Now Playing feed.
- Exclude unreleased or undated entries from cinema results while preserving pagination across filtered pages.
- Reject unsupported filter combinations rather than silently ignoring selection rules.

## 0.8.0

- Run configured discovery catalogues directly against TMDB without a hosted Discover service.
- Preserve shelf and IMDb title IDs during migration, with a persistent TMDB/IMDb mapping cache.
- Detect local selection changes and refresh without discarding availability confirmations.
- Use validated direct-API pagination to remove stale members when a catalogue shrinks, while preserving results on errors.
- Keep cached-only stream checks, complete source metadata, and direct debrid playback unchanged.

## 0.7.4

- Add optional `catalog_revision`, a manual marker for upstream selection changes.
  Changing it re-crawls all active shelves once, including curated lists, while
  retaining existing members and stream confirmations until the new crawl finishes.
- Treat an absent saved revision as the empty default on upgrade, avoiding an
  unnecessary crawl reset. Stale in-flight responses from the prior generation
  cannot overwrite the refreshed selection.

## 0.7.3

- Share episode availability evidence between explicitly matching IMDb/TMDB title
  aliases only when the episode request ID is identical. Preserve original times,
  expiry, policy scope and newer negative evidence; repair existing split evidence
  once, then keep aliases consistent during metadata refreshes and new checks.

- Add `catalog_refresh_hours` (default 6, range 1–48) for source catalogue refreshes.
  Stream checking remains continuous and uses its own confirmation/retry schedule.
- Persist the configured catalogue depth. Changing depth requeues paginated shelves
  immediately instead of waiting for the old daily refresh; existing membership
  and availability evidence are preserved until the new crawl completes.
- Re-crawl active shelves once when upgrading an index without saved crawl settings.
  Repeated profile synchronisation no longer needs to trigger another reset.
- Preserve prior shelf members when an empty upstream response could be a provider
  failure. Retry ambiguous emptiness, and reject malformed catalogue previews,
  instead of treating either as evidence that content should disappear.

## 0.7.2

- Keep separate movie and series work queues so episode batches cannot consume
  movie turns. Probe unverified shows one episode at a time across the catalogue
  before expanding already confirmed or explicitly requested titles. Expired
  historical positives rejoin ordinary scheduling instead of starving new work.
- Add a one-time administrator recovery option for still-valid confirmations
  invalidated by the 0.7.0 fingerprint migration. Import requires the same effective
  playback policy, preserves original expiry times, and never replaces newer
  conclusive checks or publishes records outside the active library. Recovery is
  idempotent and its result is reported in status.

## 0.7.1

- Prioritise pending checks and original metadata for a title opened or searched
  by a client, with a five-minute per-title cooldown. This advances discovery
  during the initial rebuild without publishing unverified results.

## 0.7.0

- Require a separate source profile for upstream catalogues and complete metadata.
  This prevents client-facing filtered episode lists feeding back into scanning and
  makes fresh installations independent of previously learned catalogue rows.
- Re-fetch original series metadata once on upgrade. The new policy fingerprint
  revalidates old verdicts on the first synchronisation; shelves refill progressively.
  Treat `max_episodes_per_series` as a progressive batch size, keeping all aired
  candidates eligible instead of permanently restricting a show to a few episodes.
- Preserve good series metadata after incomplete responses and keep episode pruning
  scoped to its parent title. Retain translated search names during catalogue
  refreshes and refresh shelves when they are re-enabled.
- Treat unexplained empty stream responses as unknown and retry them; only explicit
  negatives use `negative_hours`. Broaden policy-change detection to settings that
  affect which streams are acceptable.
- Add `stream_requests_per_minute` (default 30) for shared stream request pacing,
  with a shared cooldown when AIOStreams reports rate limiting or unavailability.
- Publish source genre information and consolidate known title aliases where the
  metadata identifies the same work.
- Correct setup and upgrade documentation: configure the separate source profile
  before switch-over, shorten AIOStreams cache lifetimes for this app's resources,
  and distinguish recent availability confirmation from a playback guarantee.
- Continue reading AIOStreams without modifying profile or administrator settings;
  no dashboard administrator credentials or video proxy are required.

## 0.6.6

- Key availability checks by `(title, episode)` instead of `(title, episode id)`.
  A show indexed under two identifiers — `tmdb:82728` from the TMDB shelves and
  `tt7678620` from the genre shelves, for example — resolves to the same episode
  ids, and the second title's checks were silently discarded by the primary key.
  That title could therefore never be verified: it stayed invisible no matter how
  many cached streams it had. Existing databases are migrated on start and series
  are re-queued once.

## 0.6.5

- On startup, re-queue series that were recorded as fetched but have no episode
  checks, so shows poisoned by the empty-list bug are retried instead of waiting up
  to a day for their next metadata refresh.

## 0.6.4

- Fix a crash in the `/status?lookup=` diagnostic (`now` was undefined).

## 0.6.3

- Never cache an episode list that yields no usable episodes. A metadata response
  with no videos (or none with an air date) was recorded as a success and kept for
  24 hours, so the show could not be verified for a day — and because the pruner
  keeps only listed episodes, it also deleted episodes that had already been
  verified. Such a response now keeps existing checks and retries in 30 minutes.
- `/status?lookup=` also reports the stored metadata's episode counts.

## 0.6.2

- Add `/status?lookup=<id>` (or `movie:<id>`) reporting why one title is or is not
  published: its shelf ranks, metadata state and per-status episode checks.

## 0.6.1

- Re-crawl once when the rank column is introduced, so existing shelves get ranks
  immediately instead of waiting up to a day for their next refresh. Verified
  results are preserved; only the crawl position is reset.

## 0.6.0

- Verify the most popular candidates first. Checks were ordered by identifier as a
  tie-breaker, so `tt0…`/`tt1…` titles were verified long before a shelf's first
  entry (`tt7678620`, i.e. Bluey, was last in its wave despite being rank 1). Titles
  now carry the best shelf rank across their shelves, and both availability checks
  and episode-list fetches order by shelf depth, then rank, then identifier.

## 0.5.0

- Treat the catalogue-source profile as authoritative and retire shelves it no
  longer offers, dropping their memberships, titles and checks. Previously the
  shelf list could only ever grow, so a revised line-up left stale, frozen rows
  behind. `metadata_pending` and the per-shelf `active` flag are reported in
  `/status`.

## 0.4.0

- Fetch metadata concurrently (`metadata_workers`) and claim titles atomically, so
  long-running shows with enormous episode lists no longer serialise the queue.
  Measured metadata throughput was the limiting factor for series.
- Order metadata breadth-first across shelves, with the curated family shelves
  first, instead of by identifier. Previously every `tmdb:` title was fetched
  before any `tt` title, so whole shelves waited behind one another.
- Make the gap between availability checks configurable
  (`check_delay_seconds`) and allow up to six check workers, so throughput can be
  matched to the AIOStreams rate limit instead of being paced by a constant.
- Report `metadata_pending` in `/status`.

## 0.3.1

- Repeat the app's own validation message in the log and in `/status`, so a
  configuration problem is readable instead of showing only the exception type.
  Provider errors still omit the text, because those messages embed request URLs
  that carry credentials.

## 0.3.0

- Fix a bootstrap deadlock: after switch-over the family manifest contains only this
  app's own catalogues, so a fresh install learned nothing and published no shelves.
  Catalogue definitions can now come from a separate, never switched-over profile
  (`catalog_uuid`/`catalog_encrypted_password`); a catalogue that finishes crawling
  empty is no longer published.
- Generate and remember `endpoint_token` when it is left empty, and print the
  manifest URL at startup, so a fresh install no longer crash-loops.
- Report incomplete configuration through `/status` instead of exiting with a
  traceback, and refuse to publish while the profile's cached-only filter is missing.

## 0.2.4

- Replace the deprecated Supervisor `watchdog` option and default `boot`/`startup`
  keys with a Docker `HEALTHCHECK`, as required by the Home Assistant add-on
  linter. The app answers `503` on `/health` until it has synchronised.

## 0.2.3

- Package the app as a standalone, publishable Home Assistant app repository.
- Add Home Assistant image labels, option translations and add-on documentation.

## 0.2.2

- Declare `idPrefixes` on the metadata resource so AIOStreams stops issuing
  unnecessary metadata lookups.

## 0.2.1

- Publish port 8097 so AIOStreams can reach the endpoint.
- Replace the boolean `watchdog` value, which made the Supervisor drop the app
  from the store, with a health-check URL.

## 0.2.0

- Read-only redesign. The app no longer writes AIOStreams configuration and no
  longer needs dashboard or administrator credentials; it indexes through the
  public Stremio endpoint and mirrors the profile's own filters.
- Filter-relevant profile changes invalidate stored verdicts; sort and label
  preferences do not.
- Cap episode checks per series and prioritise the newest seasons' openers.
- Treat HTTP 429/503 as a global back-off instead of a negative result.

## 0.1.0

- First version: background availability index with verified-only catalogues,
  filtered search, series episode checks and expiry-based rechecking.
