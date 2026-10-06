# Cached Media Library

Home Assistant app that makes an [AIOStreams](https://github.com/Viren070/AIOStreams)
Jellyfin-compatible server show **only titles that can actually play**.

Discovery is usually unfiltered: a catalogue suggests a film, but whether a
debrid service has it cached is only discovered at playback time. This app
indexes availability in the background and republishes the same catalogues and
search results containing only titles with a recently verified cached stream,
using your existing AIOStreams filters.

[![Open your Home Assistant instance and add this repository.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fbitosome%2Fha-cached-media-library)

## Requirements

- Home Assistant OS or a supervised installation (`amd64` or `aarch64`).
- The [AIOStreams add-on](https://github.com/bitosome/ha-aiostreams) with a saved
  configuration that **excludes uncached streams** and **requires**
  `cached(service(streams, 'torbox'))`. The app refuses to publish anything if
  either setting is missing, so it can never advertise unplayable titles.

## How it works

The app is **read-only with respect to AIOStreams**. It reads your profile's
settings to confirm the cache filter is still enforced, and indexes through the
public Stremio endpoint. It writes no AIOStreams configuration, stores no stream
URLs, and never proxies video.

1. Every few minutes it re-reads the profile. A change to any filter-relevant
   setting invalidates stored verdicts.
2. It learns the browsable catalogues from your profile's manifest, skipping any
   catalogue it publishes itself. Learned catalogues are never dropped, so
   indexing continues after the upstream catalogues are hidden.
3. Each candidate is checked through your own `/stream` endpoint, so cache,
   quality and language rules are identical to playback.
4. A movie is published once it has a playable HTTP stream. A series is published
   once at least one **aired** episode is confirmed, and its metadata exposes only
   confirmed episodes. Informational cards, magnets, P2P hashes and provider errors
   never count as available; timeouts are retried rather than treated as absence.
5. Verdicts expire and are refreshed, so availability stays current.

Playback is untouched: clients still stream directly from your debrid provider's
CDN, never through Home Assistant.

See [the app documentation](availability_index/DOCS.md) for the switch-over
procedure, all options and troubleshooting.

## Install

1. In Home Assistant open **Settings → Apps → App store** (older versions:
   **Add-ons**), then **Repositories**, and add
   `https://github.com/bitosome/ha-cached-media-library`.
2. Install **Cached Media Library** and fill in the options described in
   [DOCS.md](availability_index/DOCS.md).
3. Add the app's manifest URL to your AIOStreams configuration as a custom add-on,
   then hide the upstream catalogues. The documentation covers this.

## Development

The add-on is a single Python file with no third-party dependencies.

```sh
python3 -m unittest discover -s availability_index/tests -v
```

Deploy to a Home Assistant host:

```sh
scp -O -r availability_index root@HOME_ASSISTANT:/addons/
ssh root@HOME_ASSISTANT 'ha store reload && ha apps update local_availability_index'
```

The Supervisor only offers an update when `config.yaml`'s `version` increases.

## Licence

MIT — see [LICENSE](LICENSE).
