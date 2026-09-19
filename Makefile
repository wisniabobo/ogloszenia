.PHONY: install dev run scan geocode web watch test lint init sources check docker

install:
	python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt

dev:
	.venv/bin/pip install -r requirements-dev.txt

init:
	.venv/bin/python -m ogloszenia.cli init-db

sources:
	.venv/bin/python -m ogloszenia.cli sources

scan:
	.venv/bin/python -m ogloszenia.cli scan --region opolskie

geocode:
	.venv/bin/python -m ogloszenia.cli geocode --limit 500

check:
	.venv/bin/python -m ogloszenia.cli check-sources

docker:
	docker compose up -d --build

web:
	.venv/bin/python -m ogloszenia.cli web

watch:
	.venv/bin/python -m ogloszenia.cli watch

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check ogloszenia tests
