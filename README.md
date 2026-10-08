# Cached Media Library

Home Assistant app that gives an [AIOStreams](https://github.com/Viren070/AIOStreams)
Jellyfin-compatible server discovery shelves and search containing titles with
**recently confirmed cached streams**.

The app checks candidates in the background through your existing AIOStreams
filters. It publishes confirmed movies and, for series, only confirmed episodes.
Availability can change after a check; normal cached-only playback filtering still
runs when you choose a stream.

[![Open your Home Assistant instance and add this repository.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fbitosome%2Fha-cached-media-library)

## Requirements

- Home Assistant OS or a supervised installation (`amd64` or `aarch64`).
- [AIOStreams](https://github.com/bitosome/ha-aiostreams), with the playback profile
  excluding uncached streams and requiring `cached(service(streams, 'torbox'))`.
- A **separate source profile** with the original upstream catalogues and metadata
  providers enabled. Create and configure it before switching the playback profile
  to the filtered library. It must never use this app as a catalogue or metadata
  source.

## How it works

The app reads profile settings and Stremio endpoints. It does not write AIOStreams
configuration, need administrator credentials, store stream URLs, or proxy video.

1. It reads the playback profile to confirm cached-only filtering remains enforced.
   Changes to settings that affect stream acceptance invalidate stored verdicts.
2. It learns catalogues and complete metadata from the separate source profile.
   Keeping metadata independent prevents filtered episode lists feeding back into
   the scanner.
3. It checks candidates against the playback profile's `/stream` endpoint.
   Informational cards and provider failures do not establish availability. An
   unexplained empty response is retried as unknown, not cached as proof of absence.
4. It progressively checks aired episodes, giving each show a bounded batch before
   moving on. The batch setting is not a permanent episode limit. Only recently
   confirmed episodes are published.
5. Confirmations expire and are refreshed. Availability workers share request pacing and a
   cooldown after upstream rate limits.

Playback remains between the client and the debrid provider's CDN. Search covers
the indexed candidates; it is not a search of everything cached by the provider.

## Shelves

The separate source profile controls shelf names and order. Add, remove, rename or
reorder its catalogues, and the app reconciles them on the next synchronisation.
Removed shelves are retired. Re-enabled shelves are scheduled for a fresh crawl.

A useful line-up includes recent releases, trending titles, sci-fi and fantasy,
genre shelves, and curated family selections such as Лучшие мультсериалы,
Советские мультфильмы and Лучшие фильмы. The app does not curate those lists itself.
It publishes available candidates from the source profile.

Set AIOStreams `jellyfin.maxLibraries` high enough for the published shelf count.
Source shelves refresh every six hours by default (`catalog_refresh_hours`);
availability checks run continuously. Increasing catalogue depth schedules a new
crawl immediately while preserving existing confirmations.
Genre options and known title aliases are carried through where source metadata
supplies them; titles without that metadata cannot be reliably classified or merged.

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
