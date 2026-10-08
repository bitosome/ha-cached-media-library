FROM python:3.12-alpine

ARG BUILD_ARCH=unknown
ARG BUILD_VERSION=dev

LABEL \
  io.hass.name="Cached Media Library" \
  io.hass.description="Verified cached discovery and search for AIOStreams" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.type="app" \
  io.hass.version="${BUILD_VERSION}" \
  org.opencontainers.image.source="https://github.com/bitosome/ha-cached-media-library"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py tmdb_discovery.py /app/
EXPOSE 8097

# /health answers 503 until the index has synchronised with AIOStreams.
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/health', timeout=10)"]

CMD ["python", "/app/server.py"]
