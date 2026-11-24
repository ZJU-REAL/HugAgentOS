.PHONY: help install install-dev dev test selftest lint format type-check type-check-all security-scan \
	check-tool-versions \
	migrate migrate-new migrate-down migrate-history db-reset \
	build build-no-cache up up-recreate down down-volumes restart logs logs-backend logs-frontend ps \
	health-check clean clean-docker clean-all

PYTHON ?= python3
PYTEST ?= pytest
# 优先 v2 插件（docker compose），无插件时回退 v1（docker-compose）；可用 make up COMPOSE=... 覆盖
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo docker compose || echo docker-compose)
BACKEND_PORT ?= 3001
PYTHONPATH_BACKEND := src/backend

help:
	@echo "HugAgentOS Makefile Commands:"
	@echo ""
	@echo "Development:"
	@echo "  make install        - Install runtime dependencies"
	@echo "  make install-dev    - Install project with dev extras"
	@echo "  make dev            - Start backend in local dev mode"
	@echo "  make test           - Run backend test suite with coverage (needs dev extras)"
	@echo "  make selftest       - Run backend self-tests (fail fast)"
	@echo ""
	@echo "Quality:"
	@echo "  make lint           - Run format checks"
	@echo "  make format         - Format code"
	@echo "  make type-check     - Run the blocking mypy gate (same as CI)"
	@echo "  make type-check-all - Run advisory mypy over core/ and api/"
	@echo "  make security-scan  - Run bandit and safety scans"
	@echo "  make check-tool-versions - Check pinned CLI versions across Dockerfiles"
	@echo ""
	@echo "Database:"
	@echo "  make migrate        - Apply latest migrations"
	@echo "  make migrate-new    - Create migration (msg='description')"
	@echo "  make migrate-down   - Rollback one migration"
	@echo "  make migrate-history - Show migration history"
	@echo "  make db-reset       - Reset DB to latest (destructive)"
	@echo ""
	@echo "Docker:"
	@echo "  make build          - Build images"
	@echo "  make build-no-cache - Build images without cache"
	@echo "  make up             - Start containers"
	@echo "  make up-recreate    - Recreate containers"
	@echo "  make down           - Stop containers"
	@echo "  make down-volumes   - Stop and remove volumes"
	@echo "  make restart        - Restart containers"
	@echo "  make logs           - Tail all container logs"
	@echo "  make logs-backend   - Tail backend logs"
	@echo "  make logs-frontend  - Tail frontend logs"
	@echo "  make ps             - Show container status"
	@echo ""
	@echo "Utilities:"
	@echo "  make health-check   - Check /health and /ready"
	@echo "  make clean          - Remove local caches/artifacts"
	@echo "  make clean-docker   - Remove docker volumes and dangling data"
	@echo "  make clean-all      - clean + clean-docker"

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

dev:
	PYTHONPATH=$(PYTHONPATH_BACKEND) uvicorn api.app:app --reload --host 0.0.0.0 --port $(BACKEND_PORT)

test:
	PYTHONPATH=$(PYTHONPATH_BACKEND) $(PYTEST) src/backend/tests/ -v --cov=src/backend --cov-report=html --cov-report=term

selftest:
	PYTHONPATH=$(PYTHONPATH_BACKEND) $(PYTEST) src/backend/tests/ -v -x -q

lint:
	black . --line-length=100 --check
	isort . --profile black --check

format:
	black . --line-length=100
	isort . --profile black

# Keep the paths and config in sync with the type-check job in .github/workflows/ci.yml.
MYPY_GATE_PATHS := src/backend/personal_agent/ src/backend/hugagent_cli/ src/backend/features/desktop/

type-check:
	mypy --config-file mypy-gate.ini $(MYPY_GATE_PATHS)

type-check-all:
	-mypy src/backend/core/ src/backend/api/ --ignore-missing-imports

security-scan:
	bandit -r src/backend -ll -i -x src/backend/tests
	safety scan

check-tool-versions:
	$(PYTHON) scripts/check_tool_versions.py

migrate:
	alembic upgrade head

migrate-new:
	@if [ -z "$(msg)" ]; then \
		echo "Error: Please provide a message: make migrate-new msg='your message'"; \
		exit 1; \
	fi
	alembic revision --autogenerate -m "$(msg)"

migrate-down:
	alembic downgrade -1

migrate-history:
	alembic history --verbose

db-reset:
	@echo "WARNING: This will delete all data. Press Ctrl+C to cancel, or Enter to continue."
	@read confirm
	alembic downgrade base
	alembic upgrade head

build:
	$(COMPOSE) build

build-no-cache:
	$(COMPOSE) build --no-cache

up:
	$(COMPOSE) up -d

up-recreate:
	$(COMPOSE) up -d --force-recreate

down:
	$(COMPOSE) down

down-volumes:
	$(COMPOSE) down -v

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f

logs-backend:
	$(COMPOSE) logs -f backend

logs-frontend:
	$(COMPOSE) logs -f frontend

ps:
	$(COMPOSE) ps

health-check:
	@echo "Checking backend health on :$(BACKEND_PORT)..."
	@curl -fsS http://localhost:$(BACKEND_PORT)/health >/dev/null && echo "health: ok" || echo "health: failed"
	@curl -fsS http://localhost:$(BACKEND_PORT)/ready >/dev/null && echo "ready: ok" || echo "ready: failed"

clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '*.egg-info' -exec rm -rf {} +
	find . -type f -name '.coverage' -delete
	rm -rf htmlcov/ .pytest_cache/ .mypy_cache/ dist/ build/

clean-docker:
	$(COMPOSE) down -v
	docker system prune -f

clean-all: clean clean-docker
