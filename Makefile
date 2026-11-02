.PHONY: help install seed run test lint fixture self-test docker clean

help:
	@echo "install      install dependencies"
	@echo "seed         create the schema and seed a synthetic demo corpus"
	@echo "run          run the app at http://localhost:8000"
	@echo "test         run the test suite"
	@echo "fixture      ingest the GeM result fixture through the real connector"
	@echo "self-test    run every connector's self-test"
	@echo "docker       build and start with PostgreSQL"
	@echo "clean        remove the local database and archive"

install:
	pip install -r requirements.txt

seed:
	python -m scripts.sync_registry
	python -m scripts.seed_demo --tenders 60

run:
	uvicorn app.main:app --reload --port 8000

test:
	python -m pytest tests -q

fixture:
	python -m scripts.ingest_fixture gem_results

self-test:
	python -m scripts.run_connector --source gem_results --self-test
	python -m scripts.run_connector --source gem_listing --self-test

docker:
	docker compose up --build

clean:
	rm -f procintel.db procintel.db-wal procintel.db-shm
	rm -rf archive
