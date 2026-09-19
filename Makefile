.PHONY: install dev run scan web test lint init

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

web:
	.venv/bin/python -m ogloszenia.cli web

watch:
	.venv/bin/python -m ogloszenia.cli watch

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check ogloszenia tests
