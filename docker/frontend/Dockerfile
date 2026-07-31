# syntax=docker/dockerfile:1

# ============================================================================
# The frontend image — the only Node in the runtime (D4 removed the renderer).
#
# Three targets:
#   deps    — node_modules, cached on package.json alone
#   dev     — `next dev` with the host source bind-mounted (development profile)
#   runner  — standalone production server (production-local profile)
#
# Build context is the REPOSITORY ROOT, matching every other image here, so
# paths below are repo-relative.
# ============================================================================
ARG NODE_IMAGE=node:22.21-bookworm-slim


FROM ${NODE_IMAGE} AS deps
WORKDIR /app
# Only the manifests, so a source edit doesn't invalidate the install layer.
COPY apps/frontend/package.json apps/frontend/package-lock.json* ./
# `npm ci` when a lockfile exists (reproducible); `npm install` on first build
# before one is committed. The lockfile SHOULD be committed — see M0-11 notes.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi


# --------------------------------------------------------------------------- #
# Development: reload loop. Source arrives via bind mount at run time.
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS dev
ENV NODE_ENV=development \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/frontend/ ./
EXPOSE 3000
CMD ["npm", "run", "dev"]


# --------------------------------------------------------------------------- #
# Build: compile the standalone server.
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS builder
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/frontend/ ./
RUN npm run build


# --------------------------------------------------------------------------- #
# Runtime: standalone bundle only — no npm, no full node_modules, no source.
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app

# Non-root, consistent with every other image in this stack.
RUN groupadd --gid 2001 nodeapp \
 && useradd --uid 1001 --gid 2001 --create-home --shell /usr/sbin/nologin nodeapp

# `standalone` already contains the traced server and its dependencies;
# static/ and public/ are served alongside it.
COPY --from=builder --chown=nodeapp:nodeapp /app/.next/standalone ./
COPY --from=builder --chown=nodeapp:nodeapp /app/.next/static ./.next/static

USER nodeapp
EXPOSE 3000
CMD ["node", "server.js"]
