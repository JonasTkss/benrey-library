.PHONY: install run test lint fmt seed check

install:
	pip install -r requirements-dev.txt && pre-commit install

run:
	python manage.py runserver

test:
	pytest --cov

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .

seed:
	python manage.py seed_demo --reset --seed 42

check:
	python manage.py check && python manage.py makemigrations --check --dry-run
