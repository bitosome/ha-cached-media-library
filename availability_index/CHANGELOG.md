# Changelog

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
- Learned catalogues are never dropped, so indexing continues after upstream
  catalogues are hidden at switch-over.
- Filter-relevant profile changes invalidate stored verdicts; sort and label
  preferences do not.
- Cap episode checks per series and prioritise the newest seasons' openers.
- Treat HTTP 429/503 as a global back-off instead of a negative result.

## 0.1.0

- First version: background availability index with verified-only catalogues,
  filtered search, series episode checks and expiry-based rechecking.
