# syntax note: build from the repo root (docker-compose.yml sets
# `context: .`, `dockerfile: frontend/Dockerfile`).
#
# Stages:
#   deps        -- shared dependency install, used by both dev and build.
#   dev         -- byte-for-byte today's behavior (Vite dev server). This is
#                  the stage docker-compose.yml pins via `target: dev`, so
#                  `docker compose up` is unaffected by anything below.
#   build       -- compiles production static assets (tsc -b && vite build).
#   production  -- nginx serving the built assets with
#                  frontend/nginx.conf.template's security headers. Not wired
#                  into docker-compose.yml; built and verified independently
#                  (see tests/test_production_security.py's container
#                  assertion).

FROM node:22.17.0-alpine AS deps
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.12.4 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json ./frontend/package.json
RUN pnpm install --filter @ecc/frontend --frozen-lockfile

FROM deps AS dev
COPY --chown=node:node frontend ./frontend
USER node
CMD ["pnpm", "--filter", "@ecc/frontend", "dev"]

FROM deps AS build
COPY frontend ./frontend
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN pnpm --filter @ecc/frontend build

FROM nginx:1.27-alpine AS production
# The nginx:1.27-alpine tag already resolves to the newest published
# 1.27.x-alpine build, but Alpine ships OS-package security fixes via its
# apk repository ahead of new base-image tags/rebuilds. Pull those fixes in
# explicitly so the image isn't stuck on stale libcrypto3/libssl3/libxml2/etc
# versions between upstream base-image rebuilds.
RUN apk update && apk upgrade --no-cache
# Copied into /etc/nginx/templates/ (not conf.d/ directly) so the base
# image's own docker-entrypoint.d/20-envsubst-on-templates.sh renders it into
# /etc/nginx/conf.d/default.conf at container start, substituting
# ${VITE_API_BASE_URL} into the CSP's connect-src -- see the template's own
# header comment for why this must be a runtime value, not just baked in at
# build time like the JS bundle's copy of the same variable.
COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/frontend/dist /usr/share/nginx/html
# Default so the container still starts (with a same-origin-only CSP) if a
# deployer forgets to pass -e VITE_API_BASE_URL at `docker run` time; the
# actual production value must be supplied at run time to match whatever was
# baked into the JS bundle via the build-stage ARG above.
ENV VITE_API_BASE_URL=""
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
