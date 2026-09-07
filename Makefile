export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv-python

.PHONY: install test test-ddb lint typecheck run build live-check live-check-09a live-check-09b live-check-10 live-check-11b live-check-11c live-check-11d

install:
	uv sync --locked

test:
	uv run --offline --no-sync pytest $(TEST_ARGS)

test-ddb:
	uv run --offline --no-sync pytest tests/store tests/deploy --ddb --require-ddb

lint:
	uv run --offline --no-sync ruff check .
	uv run --offline --no-sync ruff format --check .

typecheck:
	uv run --offline --no-sync mypy

run:
	uv run --offline --no-sync uvicorn sanad.api.app:create_app --factory --host 127.0.0.1 --port 8000

build:
	uv build --offline --no-build-isolation

live-check:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract08_live

live-check-09a:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract09a_live

live-check-09b:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract09b_live

live-check-10:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract10_live

live-check-11b:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract11b_live

live-check-11c:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract11c_live

live-check-11d:
	@test "$$SANAD_LIVE" = "1" || (echo 'SANAD_LIVE=1 is required'; exit 1)
	uv run --offline --no-sync pytest -o addopts='-q -s' tests/live --live -k contract11d_live
