# Changelog

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
- Learned catalogues are never dropped, so indexing continues after upstream
  catalogues are hidden at switch-over.
- Filter-relevant profile changes invalidate stored verdicts; sort and label
  preferences do not.
- Cap episode checks per series and prioritise the newest seasons' openers.
- Treat HTTP 429/503 as a global back-off instead of a negative result.

## 0.1.0

- First version: background availability index with verified-only catalogues,
  filtered search, series episode checks and expiry-based rechecking.
