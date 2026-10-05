SHELL := /bin/bash
.DEFAULT_GOAL := run

.PHONY: run test evaluate web digest

# Interactive wrapper around `python -m app.main` — asks the questions
# instead of you having to remember the argparse flags.
run:
	@companies_flag=""; aggregators_flag=""; discovery_flag=""; company_flag=""; \
	read -rp "Fetch from companies.yaml? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && companies_flag="--skip-companies"; \
	read -rp "Fetch from aggregators.yaml? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && aggregators_flag="--skip-aggregators"; \
	read -rp "Auto-discover new companies from Adzuna results? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && discovery_flag="--skip-discovery"; \
	read -rp "Only one company slug? (blank = all) " slug; \
	[[ -n "$$slug" ]] && company_flag="--company $$slug"; \
	python -m app.main $$companies_flag $$aggregators_flag $$discovery_flag $$company_flag

# Automated test suite: pytest-style tests plus the standalone main()-style
# sanity scripts that print their own pass/fail summary.
test:
	python -m pytest app/ -q
	python -m app.tests.test_pipeline
	python -m app.tests.test_discover_companies
	python -m app.tests.test_ai_evaluate

# Interactive wrapper around `python -m app.ai_evaluate` — asks the questions
evaluate:
	@dry_run_flag=""; limit_flag=""; \
	read -rp "Do you want a dry run? [Y/n] " ans; \
	[[ ! "$$ans" =~ ^[Nn] ]] && dry_run_flag="--dry-run"; \
	read -rp "Do you want to set a limit? (blank = all) " limit_val; \
	[[ -n "$$limit_val" ]] && limit_flag="--limit $$limit_val"; \
	python -m app.ai_evaluate $$dry_run_flag $$limit_flag

web:
	DB_PATH=$(CURDIR)/data/seen_jobs.sqlite3 npm --prefix web run dev

# Markdown digest of jobs scored 55 or above that are not marked applied
# or skipped, highest score first. Applied/Skipped edits in an existing
# digest are saved first.
digest:
	python -m app.digest
