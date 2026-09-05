export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv-python

.PHONY: install test lint typecheck run build

install:
	uv sync --locked

test:
	uv run --offline --no-sync pytest

lint:
	uv run --offline --no-sync ruff check .
	uv run --offline --no-sync ruff format --check .

typecheck:
	uv run --offline --no-sync mypy

run:
	uv run --offline --no-sync uvicorn sanad.api.app:create_app --factory --host 127.0.0.1 --port 8000

build:
	uv build --offline --no-build-isolation
