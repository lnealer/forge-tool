# forge-tool
#
# Thin wrappers around the scripts in infra/. Everything is configured through
# the repo-root .env file (see .env.example).

SHELL := /bin/bash

.DEFAULT_GOAL := help
.PHONY: help setup install run start stop restart status config check secrets infra infra-plan infra-guardrail infra-permissions infra-sync infra-destroy clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies then start the Streamlit server
	./infra/setup.sh all

install: ## Create the virtualenv and install python/requirements.txt
	./infra/setup.sh install

run: ## Launch the Streamlit application locally
	./infra/setup.sh run

start: ## Start the Streamlit server detached (logs to app.log)
	./infra/setup.sh start

stop: ## Stop the server; refuses while a browser is connected or a run is active (FORCE=1 overrides)
	./infra/setup.sh stop $(if $(FORCE),--force,)

restart: ## Stop + start, same guard (FORCE=1 overrides)
	./infra/setup.sh restart $(if $(FORCE),--force,)

status: ## Server pid, uptime, health, connected browsers, recent activity
	./infra/setup.sh status

config: ## Print the effective configuration (paths, ids, branches, limits) and where each value came from
	./infra/setup.sh config

check: ## Verify toolchain, AWS credentials, knowledge base and secrets
	./infra/setup.sh check

secrets: ## Store the GitHub PAT in SSM Parameter Store
	./infra/put_ssm_parameters.sh

infra: ## Terraform apply: knowledge base, guardrail, permissions
	./infra/deploy.sh

infra-plan: ## Terraform plan only, change nothing
	./infra/deploy.sh --plan

infra-guardrail: ## Apply only the Bedrock guardrail
	./infra/deploy.sh --guardrail-only

infra-permissions: ## Apply only the IAM runtime policy
	./infra/deploy.sh --permissions-only

infra-sync: ## Upload knowledge-base/ documents and start an ingestion job
	./infra/deploy.sh --sync-only

infra-destroy: ## Terraform destroy everything (buckets included; PAT in SSM kept)
	./infra/deploy.sh --delete

clean: ## Remove Python cache and test artifacts
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .coverage
