ARG UPTIME_KUMA_VERSION=2.5.6
FROM louislam/uptime-kuma:${UPTIME_KUMA_VERSION}

LABEL org.opencontainers.image.title="Uptime Kuma Operations"
LABEL org.opencontainers.image.description="Uptime Kuma 2.5.6 with an Operations UI and custom monitor features"
LABEL org.opencontainers.image.source="https://github.com/hoangankg797521/Docker-Source/tree/main/uptime-kuma-operations"

WORKDIR /app
ENV UPTIME_KUMA_IS_CONTAINER=1

# Overlay only the customized runtime source, Knex migration, and production UI bundle.
COPY --chown=node:node overlay/app/ /app/

# Fail the image build if a custom backend module or migration is malformed.
RUN node --check server/model/monitor.js \
    && node --check server/server.js \
    && node --check server/all-online-notifier.js \
    && node --check db/knex_migrations/2026-10-15-0000-add-additional-monitor-checks.js \
    && test -s dist/index.html
