# Repository-owned verification. `make verify-all` is the local gate.
# Governance targets run the engine that PRAETOR_REF pins, resolved by engine.mk.
-include .config/praetor/engine.mk
PYTHON := python
ACTIONLINT := actionlint
.PHONY: verify-all compile-context compile-context-verify caveman-sources audit lint policy test workflows

verify-all: compile-context-verify caveman-sources audit lint policy test workflows

compile-context:
	@$(PRAETORCTL) compile-context

compile-context-verify:
	@$(PRAETORCTL) compile-context --verify

caveman-sources:
	@$(PRAETORCTL) caveman check --configured-sources

audit:
	@$(PRAETORCTL) audit

lint:
	@$(PYTHON) -m ruff check .
	@$(PYTHON) -m ruff format --check .

policy:
	@$(PYTHON) tools.py check-policy

# The coverage gate (fail_under in pyproject.toml) fails this target.
test:
	@$(PYTHON) -m coverage run -m unittest discover -s tests
	@$(PYTHON) -m coverage report

workflows:
	@$(ACTIONLINT)
