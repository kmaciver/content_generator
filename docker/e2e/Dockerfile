# syntax=docker/dockerfile:1

# Playwright runner.
#
# A separate image from the frontend's, because the browser bundle is ~1GB of
# Chromium that the dev and prod frontend images have no reason to carry.
#
# Based on Microsoft's official Playwright image rather than installing
# browsers into a Node base: the browsers need a long tail of system libraries
# (nss, dbus, at-spi, fonts) whose absence surfaces as a browser that launches
# and then dies with no useful message. The tag MUST match the @playwright/test
# version in apps/frontend/package.json — a mismatch between the driver and the
# bundled browsers is a documented failure mode, and Playwright reports it as a
# vague launch error rather than a version complaint.
FROM mcr.microsoft.com/playwright:v1.62.1-noble

ENV CI=true \
    # Browsers are baked into the image at a known path; without this,
    # Playwright looks under the mounted node_modules and finds nothing.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# No CMD: the Makefile supplies the command, matching the tooling image.

# --------------------------------------------------------------------------- #
# Dependencies, baked (M5-12)
# --------------------------------------------------------------------------- #
#
# The suite used to run `npm install` on every invocation, into an anonymous
# volume that `--rm` then threw away — 370 packages fetched per run. That is
# slow on a good day and fatal on a bad one: two consecutive runs died with
# ECONNRESET partway through the download, on a machine where DNS resolved and
# a plain HTTPS GET to the registry returned 200. An end-to-end suite that
# cannot start without a healthy connection to a package registry is testing
# the registry.
#
# `npm ci` rather than `npm install`: the lockfile is the point — the driver
# version here must match the browsers baked into the base image above, and
# `install` is free to resolve a newer one.
#
# Copied lockfile-first so the layer is cached against it. Changing a spec file
# does not refetch anything; changing a dependency does, once.
COPY apps/frontend/package.json apps/frontend/package-lock.json /app/
RUN npm ci --no-audit --no-fund

# The Makefile bind-mounts the host's `apps/frontend` over /app, which would
# hide this. It also passes `-v /app/node_modules`, an anonymous volume that
# Docker seeds from *this* path in the image — so the mount shadows the source
# and the volume restores the dependencies.
