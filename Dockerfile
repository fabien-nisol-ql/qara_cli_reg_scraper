# --- stage 1: build supercronic --------------------------------------------
# supercronic (https://github.com/aptible/supercronic) is a cron built for
# containers: no root/syslog requirement, logs to stdout, PID 1 friendly.
# Building it from source via `go install` (module-proxy checksum verified)
# avoids pinning a downloaded binary's sha256 by hand.
FROM golang:1.22-alpine AS cronbuilder
RUN go install github.com/aptible/supercronic@v0.2.29

# --- stage 2: the actual image ----------------------------------------------
FROM python:3.13-slim AS base

# boto3/azure-storage-blob/Office365-REST-Python-Client (and requests, this
# CLI's only way to reach qara-reg-scraper-svc — no DB driver at all here)
# are all pure-Python or ship binary wheels, so no compiler toolchain is
# needed — keeps the image small and avoids the usual gcc build layer.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[all-storage]'

COPY --from=cronbuilder /go/bin/supercronic /usr/local/bin/supercronic
COPY docker/crontab /etc/qara-reg-scraper/crontab
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Non-root on purpose: unlike traditional cron, supercronic doesn't need
# root to run, so there's no reason this container should have it.
RUN useradd --create-home --uid 1000 scraper \
    && mkdir -p /app/data /app/.locks /app/logs \
    && chown -R scraper:scraper /app
USER scraper

# Baseline config; override at runtime by mounting your own config.yaml and
# .env (see docker-compose.yml) or passing QARA_REG_SCRAPER_* env vars.
ENV QARA_REG_SCRAPER_LOG_LEVEL=INFO

ENTRYPOINT ["/entrypoint.sh"]
# Default: run as the in-container scheduler (see docker/crontab). Override
# the command for a one-off run, e.g.:
#   docker run --rm --env-file .env -v $(pwd)/data:/app/data \
#     qara-reg-scraper run --source fda:ecfr
CMD ["scheduler"]
