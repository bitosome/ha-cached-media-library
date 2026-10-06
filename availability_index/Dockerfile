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
COPY server.py /app/server.py
EXPOSE 8097
CMD ["python", "/app/server.py"]
