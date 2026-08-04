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
