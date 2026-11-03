# ha-cached-media-library

Supervisor app (slug `availability_index`) that gives AIOStreams' Jellyfin-compatible server shelves and search of recently confirmed cached titles.

<!-- workspace-pointer -->
## Shared workspace rules

Part of the bitosome Home Assistant workspace. Shared rules, access (SSH, ha-mcp) and processes
are in `../AGENTS.md` (the workspace entry point) and `../home-assistant-knowledge/`; they take
precedence over this file. Build, test, release and known gotchas for this repo:
[repo catalog](../home-assistant-knowledge/docs/repo-catalog.md#ha-cached-media-library).

- Edit and release from this checkout; never patch production directly.
- Push, tag/release, HACS install, production changes, reloads and restarts need the owner's explicit approval.
- After a release or install, update `docs/current-state.md` and `docs/changelog.md` in the knowledge repo.
