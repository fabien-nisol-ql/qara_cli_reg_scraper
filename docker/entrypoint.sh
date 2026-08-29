#!/bin/sh
# Two modes, one image:
#   docker run qara-reg-scraper                 -> "scheduler" (default CMD):
#       runs supercronic in the foreground against docker/crontab, i.e. the
#       container itself is the daily schedule — `docker run -d` it once
#       and it keeps running/scraping on the built-in cron.
#   docker run qara-reg-scraper run --source X   -> any other command is
#       exec'd straight through to the qara-reg-scraper CLI, for one-off
#       runs (`docker run --rm ... qara-reg-scraper reindex --source all`).
set -eu

if [ "$#" -eq 0 ] || [ "$1" = "scheduler" ]; then
    echo "qara-reg-scraper: starting supercronic (${CRONTAB_FILE:-/etc/qara-reg-scraper/crontab})"
    exec supercronic -json "${CRONTAB_FILE:-/etc/qara-reg-scraper/crontab}"
fi

exec qara-reg-scraper "$@"
