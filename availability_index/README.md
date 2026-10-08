# Cached Media Library

Discovery and search for an AIOStreams Jellyfin-compatible server, limited to
titles with recently confirmed cached streams. Availability may change between a
background check and playback; the playback profile still enforces cached-only
stream filtering.

Optional direct TMDB discovery builds catalogues inside the app. A separate
profile supplies other unfiltered catalogues and complete metadata. The app only reads
AIOStreams, publishes catalogues and metadata, and never serves or proxies video.
See [the documentation](DOCS.md) before installing or upgrading to 0.7.0.
