FROM docker.io/library/node:krypton-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS ui
WORKDIR /src
RUN npm install --global corepack@0.34.6 && corepack enable
COPY src/package.json src/pnpm-lock.yaml src/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY src ./
RUN pnpm typecheck && pnpm build
FROM ghcr.io/wg-easy/wg-easy:15.4.0@sha256:0e7bc9d34e86ddcaa92bc700d4d7dc9b33291dbc07ac8d13382f7c2095f949ec
COPY --from=ui /src/.output /app
COPY --from=ui /src/server/database/migrations /app/server/database/migrations

LABEL org.opencontainers.image.source="https://github.com/hoangankg797521/Docker-Source" org.opencontainers.image.licenses="AGPL-3.0-only"
