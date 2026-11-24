.PHONY: install install-dev run test docker-build up down clean

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt -r requirements-dev.txt

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	pytest

docker-build:
	docker compose build

up:
	docker compose up --build

down:
	docker compose down

clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
