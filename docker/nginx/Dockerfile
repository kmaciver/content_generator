# syntax=docker/dockerfile:1

# M0-00 leaves this tag floating; M0-03 pins it (by digest) along with the rest
# of the compose topology. The spike's subject is uWSGI, not nginx.
FROM nginx:alpine

# Join the shared socket group so nginx workers can reach the uWSGI socket at
# mode 660. gid 2000 must match apps/backend/Dockerfile.
RUN addgroup -g 2000 uwsgisock \
 && addgroup nginx uwsgisock

COPY nginx.conf /etc/nginx/nginx.conf

# No build-time `nginx -t` since M0-10: the config now declares
# `upstream s3 { server minio:9000; }`, and upstream hostnames are resolved at
# config parse — minio doesn't exist in the build environment, so -t would fail
# on every build. A malformed config still fails loudly: nginx refuses to start
# and the compose service_healthy gate reports it within seconds.
