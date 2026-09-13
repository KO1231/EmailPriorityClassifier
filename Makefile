.DEFAULT_GOAL := help

# --- Development ----------------------------------------------------------

.PHONY: install
install: ## Sync the environment from uv.lock and install the git hooks
	uv sync --all-extras
	uv run pre-commit install

.PHONY: lint
lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

.PHONY: format
format: ## Apply formatting and autofixable lint rules
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: typecheck
typecheck: ## Static type check
	uv run mypy

.PHONY: test
test: ## Run the test suite (excludes the opt-in eval suite)
	uv run pytest -m "not eval"

.PHONY: test-coverage
test-coverage: ## Run tests with an HTML coverage report
	uv run pytest -m "not eval" --cov --cov-report=html
	@echo "Open htmlcov/index.html"

.PHONY: check
check: lint typecheck test ## Everything CI runs

.PHONY: audit
audit: ## Check locked dependencies against known vulnerabilities
	uv export --all-extras --no-emit-project --format requirements.txt > /tmp/epc-requirements.txt
	uvx pip-audit --requirement /tmp/epc-requirements.txt --strict

.PHONY: lock-refresh
lock-refresh: ## Move the supply-chain cooldown window forward, then re-lock
	@echo "Set [tool.uv] exclude-newer in pyproject.toml to a date at least 7 days old,"
	@echo "then run 'uv lock' and read the uv.lock diff before committing."

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
