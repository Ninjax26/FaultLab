.PHONY: install run-api run-worker migrate test test-integration lint format check compose-up compose-down stage1-db stage1-benchmark benchmark-db stage5-benchmark go-test go-build stage6-benchmark stage6-pressure

install:
	uv sync --no-editable

run-api:
	uv run --no-editable uvicorn faultlab.api.main:app --reload

run-worker:
	uv run --no-editable python -m faultlab.worker.main

migrate:
	uv run --no-editable alembic upgrade head

test:
	uv run --no-editable pytest

test-integration:
	docker compose --profile test up -d --wait postgres-test
	TEST_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test uv run --no-editable pytest tests/integration

lint:
	uv run --no-editable ruff check .

format:
	uv run --no-editable ruff format .
	uv run --no-editable ruff check --fix .

check:
	uv run --no-editable ruff check .
	uv run --no-editable mypy
	uv run --no-editable pytest

compose-up:
	docker compose up --build

compose-down:
	docker compose down

stage1-db:
	docker compose up -d --wait postgres
	uv run --no-editable alembic upgrade head

stage1-benchmark: stage1-db
	uv run --no-editable python scripts/benchmark_claiming.py

benchmark-db:
	docker compose --profile test up -d --wait postgres-test
	FAULTLAB_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test uv run --no-editable alembic upgrade head

stage5-benchmark: benchmark-db
	TEST_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test PYTHONPATH=src uv run --no-editable python scripts/benchmark_pipeline.py

go-test:
	cd go-worker && go test ./...

go-build:
	cd go-worker && go build -o faultlab-go-worker .

stage6-benchmark: benchmark-db go-build
	TEST_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test PYTHONPATH=src uv run --no-editable python scripts/benchmark_runtimes.py --go-binary go-worker/faultlab-go-worker

stage6-pressure: benchmark-db go-build
	TEST_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test PYTHONPATH=src uv run --no-editable python scripts/benchmark_connection_pressure.py --go-binary go-worker/faultlab-go-worker
