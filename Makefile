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
	uvx pip-audit@2.10.1 --disable-pip --require-hashes --requirement /tmp/epc-requirements.txt --strict

.PHONY: lock-refresh
lock-refresh: ## Move the supply-chain cooldown window forward, then re-lock
	@echo "Set [tool.uv] exclude-newer in pyproject.toml to a date at least 7 days old,"
	@echo "then run 'uv lock' and read the uv.lock diff before committing."

# --- Container ------------------------------------------------------------

.PHONY: docker-build
docker-build: ## Build the container image
	docker build -t epc:local .

.PHONY: docker-run
docker-run: ## Dry run inside the container, with the local config mounted
	docker compose run --rm epc run --dry-run

# --- Infrastructure -------------------------------------------------------
# Same shape as the rest of the estate: init-{env} / plan-{env} / apply-{env},
# with a plan file in between so apply does exactly what was reviewed.

.PHONY: tf-fmt
tf-fmt: ## Format the Terraform
	terraform fmt -recursive environments/ modules/

.PHONY: tf-validate
tf-validate: ## Validate every environment without touching a backend
	@for env in environments/*/; do \
		echo "== $$env"; \
		terraform -chdir=$$env init -backend=false >/dev/null && \
		terraform -chdir=$$env validate || exit 1; \
	done

init-%: ## Initialise an environment (make init-dev)
	@test -d environments/$* || { echo "No such environment: $*"; exit 1; }
	@test -f environments/$*/config.yml || { echo "environments/$*/config.yml is missing"; exit 1; }
	cd environments/$* && terraform init

plan-%: ## Plan, saving the plan so apply can replay exactly it
	@test -d environments/$*/.terraform || { echo "Run 'make init-$*' first"; exit 1; }
	rm -f environments/$*/.plan
	cd environments/$* && terraform plan -out .plan

apply-%: ## Apply a saved plan, after showing it
	@test -f environments/$*/.plan || { echo "Run 'make plan-$*' first"; exit 1; }
	cd environments/$* && terraform show .plan && terraform apply .plan && rm -f .plan

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
