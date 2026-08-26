.PHONY: clean clean-test clean-pyc clean-build help
.DEFAULT_GOAL := help

.PHONY: install
install: ## install the project and dependencies into a uv-managed venv
	uv sync

.PHONY: run-tests
run-tests: install ## run the test suite
	uv run pytest tests

clean: clean-build clean-pyc clean-test ## remove all build, test, coverage and Python artifacts

clean-build: ## remove build artifacts
	rm -fr build/
	rm -fr dist/
	find . -name '*.egg-info' -exec rm -fr {} +

clean-pyc: ## remove Python file artifacts
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

clean-test: ## remove test and coverage artifacts
	rm -fr .pytest_cache

dist: clean ## build source and wheel package
	uv build
	ls -l dist
