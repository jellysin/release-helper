# Repository-owned verification. `make verify-all` is the local gate.
# Governance targets run the engine that PRAETOR_REF pins, resolved by engine.mk.
-include .config/praetor/engine.mk
# Development tools run from the virtual environment that `make bootstrap` builds from the
# hash-pinned requirements-dev.txt; a missing or stale one fails, never falls back to the host
# python. The pre-push gate runs in a temporary worktree and passes the checkout's DEV_VENV.
DEV_VENV ?= .venv
PYTHON := $(DEV_VENV)/bin/python
ACTIONLINT := actionlint
.PHONY: verify-all bootstrap python-env compile-context compile-context-verify caveman-sources audit lint policy test workflows

verify-all: compile-context-verify caveman-sources audit lint policy test workflows

bootstrap:
	$(if $(filter .venv %/.venv,$(DEV_VENV)),,$(error DEV_VENV must name a .venv directory, not $(DEV_VENV)))
	@python3 -m venv --clear $(DEV_VENV)
	@$(PYTHON) -m pip install --quiet --timeout 60 --require-hashes -r requirements-dev.txt
	@cp requirements-dev.txt $(DEV_VENV)/requirements-dev.txt

python-env:
	@test -x $(PYTHON) || { echo "$(PYTHON) is missing: run 'make bootstrap'" >&2; exit 1; }
	@cmp -s requirements-dev.txt $(DEV_VENV)/requirements-dev.txt || \
		{ echo "$(DEV_VENV) does not match requirements-dev.txt: run 'make bootstrap'" >&2; exit 1; }

compile-context:
	@$(PRAETORCTL) compile-context

compile-context-verify:
	@$(PRAETORCTL) compile-context --verify

caveman-sources:
	@$(PRAETORCTL) caveman check --configured-sources

audit:
	@$(PRAETORCTL) audit

lint: python-env
	@$(PYTHON) -m ruff check .
	@$(PYTHON) -m ruff format --check .

policy: python-env
	@$(PYTHON) tools.py check-policy

# The coverage gate (fail_under in pyproject.toml) fails this target.
test: python-env
	@$(PYTHON) -m coverage run -m unittest discover -s tests
	@$(PYTHON) -m coverage report

workflows:
	@$(ACTIONLINT)
