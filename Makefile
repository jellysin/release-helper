# Repository-owned verification. `make verify-all` is the local gate.
# Governance targets run the engine that PRAETOR_REF pins, resolved by engine.mk.
-include .config/praetor/engine.mk
# Development tools run from the virtual environment that `make bootstrap` builds from the
# hash-pinned requirements-dev.txt; a missing or stale one fails, never falls back to the host
# python. The pre-push gate runs in a temporary worktree and passes the checkout's DEV_VENV.
DEV_VENV ?= .venv
export DEV_VENV
DIST_PYTHON ?= python3
ACTIONLINT := actionlint
.PHONY: verify-all bootstrap python-env compile-context compile-context-verify caveman-sources audit release-audit release-audit-replay lint policy test workflows dist

verify-all: compile-context-verify caveman-sources audit release-audit release-audit-replay lint policy test workflows

bootstrap:
	@case "$$DEV_VENV" in .venv|*/.venv) ;; *) echo "DEV_VENV must name a .venv directory, not $$DEV_VENV" >&2; exit 1;; esac
	@python3 -m venv --clear "$$DEV_VENV"
	@"$$DEV_VENV/bin/python" -m pip install --quiet --timeout 60 --require-hashes -r requirements-dev.txt
	@cp requirements-dev.txt "$$DEV_VENV/requirements-dev.txt"

python-env:
	@test -x "$$DEV_VENV/bin/python" || { echo "$$DEV_VENV/bin/python is missing: run 'make bootstrap'" >&2; exit 1; }
	@cmp -s requirements-dev.txt "$$DEV_VENV/requirements-dev.txt" || \
		{ echo "$$DEV_VENV does not match requirements-dev.txt: run 'make bootstrap'" >&2; exit 1; }

compile-context:
	@$(PRAETORCTL) compile-context

compile-context-verify:
	@$(PRAETORCTL) compile-context --verify

caveman-sources:
	@$(PRAETORCTL) caveman check --configured-sources

audit:
	@$(PRAETORCTL) audit

# The release pull request audits in release context, where end-to-end evidence and release
# test level gaps fail instead of warning. Running it here refuses them before the release.
# The local check runs offline; the release pull request judges live workflow run health, so
# a red main branch never refuses the push that repairs it.
release-audit:
	@PRAETOR_RELEASE_PR=true $(PRAETORCTL) audit --offline

release-audit-replay: python-env
	@"$$DEV_VENV/bin/python" tests/release_audit_replay.py "$(PRAETORCTL)"

lint: python-env
	@"$$DEV_VENV/bin/python" -m ruff check .
	@"$$DEV_VENV/bin/python" -m ruff format --check .

policy: python-env
	@"$$DEV_VENV/bin/python" tools.py check-policy

# The coverage gate (fail_under in pyproject.toml) fails this target.
test: python-env
	@"$$DEV_VENV/bin/python" -m coverage run -m unittest discover -s tests
	@"$$DEV_VENV/bin/python" -m coverage report

workflows:
	@$(ACTIONLINT)

dist:
	$(DIST_PYTHON) tools.py package-source --repo "$(RELEASE_REPO)" --tag "$(RELEASE_TAG)" --output-directory dist
