.PHONY: install fmt lint test audit dev up down logs

install:
	pip install -r requirements.txt -r requirements-dev.txt

fmt:
	ruff format .

lint:
	ruff check .

test:
	pytest -q

audit:
	./run_audit_tests.sh

dev:
	uvicorn fakturek.main:app --reload --port 8000

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f app db
