PYTHON ?= python3
VENV ?= .venv

.DEFAULT_GOAL := help

VERSION := $(shell $(VENV)/bin/python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" 2>/dev/null || echo "0.0.0")

# --- CLI pass-through ---------------------------------------------------
#
# `run` and `drun` are the only two ways this Makefile runs the project —
# `run` locally (venv), `drun` in Docker. Neither wraps or renames
# anything: everything after `--` goes straight to the real
# qara-reg-scraper CLI, subcommand included, unmodified. `make run --
# --help` (etc.) shows the CLI's actual options, not a Makefile guess at
# them.
#
#   make run -- run --source fda:ecfr    ->  qara-reg-scraper run --source fda:ecfr
#   make run -- reindex --source all     ->  qara-reg-scraper reindex --source all
#   make run -- status --source all      ->  qara-reg-scraper status --source all
#   make run -- list-sources             ->  qara-reg-scraper list-sources
#   make drun -- run --source fda:ecfr   ->  docker compose run --rm qara-reg-scraper run --source fda:ecfr
#
# ARGS is computed positionally (drop MAKECMDGOALS' first word, then any
# "--"), not by matching target names — qara-reg-scraper's own "run"
# subcommand happens to share a name with the "run" Make target, so
# name-based filtering would incorrectly strip both.
#
# Known cosmetic quirk: because of that same name collision, `make run --
# run ...` prints an extra "make: Nothing to be done for `run'." line after
# the real command already ran (correctly — check the "+ ..." echo line
# above it). That's Make's own goal-deduplication for the literal word
# "run" appearing twice in one invocation, not a qara-reg-scraper error;
# `drun` doesn't have this since no qara-reg-scraper subcommand is named
# "drun".
ARGS := $(filter-out --,$(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS)))

.PHONY: help venv install install-dev run drun lint test build docker-build release up down logs clean distclean

help:
	@echo "Running the project:"
	@echo "  make run -- <cli args>    e.g. make run -- run --source fda:ecfr"
	@echo "  make drun -- <cli args>   same, inside Docker (docker compose run --rm)"
	@echo "  make run -- --help        see qara-reg-scraper's own top-level help"
	@echo ""
	@echo "Development:"
	@echo "  make venv           Create local virtualenv"
	@echo "  make install        Install package in editable mode"
	@echo "  make install-dev    Install package with dev tools (+ all storage extras)"
	@echo "  make lint           Run ruff checks"
	@echo "  make test           Run tests"
	@echo ""
	@echo "Build / release:"
	@echo "  make build          Build wheel + sdist into dist/"
	@echo "  make docker-build   Build the Docker image (tag: qara-reg-scraper:latest)"
	@echo "  make release        build + docker-build, tag docker image with pyproject's"
	@echo "                      version ($(VERSION)) — local artifacts only, nothing is"
	@echo "                      pushed anywhere (no registry/PyPI configured yet)"
	@echo ""
	@echo "The always-on Docker scheduler (supercronic + docker/crontab, NOT what"
	@echo "run/drun do — those are one-off CLI invocations):"
	@echo "  make up             Build + start the scheduler container (detached)"
	@echo "  make logs           Follow its logs"
	@echo "  make down           Stop it"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean          Remove build/test caches"
	@echo "  make distclean      Remove venv, dist, data, locks, caches"

venv:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip

install: venv
	$(VENV)/bin/pip install -e .

install-dev: venv
	$(VENV)/bin/pip install -e '.[dev,all-storage]'

run: install
	@echo "+ $(VENV)/bin/qara-reg-scraper $(ARGS)"
	@$(VENV)/bin/qara-reg-scraper $(ARGS)

drun: docker-build
	@echo "+ docker compose run --rm qara-reg-scraper $(ARGS)"
	@docker compose run --rm qara-reg-scraper $(ARGS)

lint: install-dev
	$(VENV)/bin/ruff check .

test: install-dev
	$(VENV)/bin/pytest

build: install-dev
	$(VENV)/bin/python -m build

docker-build:
	docker compose build

release: build docker-build
	@docker tag qara-reg-scraper:latest qara-reg-scraper:$(VERSION)
	@echo "Release artifacts ready (local only — nothing pushed, no registry configured):"
	@echo "  dist/                           wheel + sdist, version $(VERSION)"
	@echo "  qara-reg-scraper:$(VERSION)      docker image (also tagged :latest)"

up: docker-build
	docker compose up -d

logs:
	docker compose logs -f qara-reg-scraper

down:
	docker compose down

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV) data .locks logs/*.log

# Swallow anything after `--` (e.g. `run`, `--source`, `fda:ecfr`) so Make
# doesn't try to interpret those words as target names of their own
# ("No rule to make target 'run'"). Must stay the last rule in this file.
%:
	@:
