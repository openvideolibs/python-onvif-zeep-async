clean: clean-build clean-pyc clean-test ## remove all build, test, coverage and Python artifacts

clean-build: ## remove build artifacts
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -fr {} +

clean-pyc: ## remove Python file artifacts
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

clean-test: ## remove test and coverage artifacts
	rm -fr .tox/
	rm -f .coverage
	rm -fr htmlcov/

lint: ## check style with flake8
	flake8 onvif tests
	pylint onvif

test: ## run tests quickly with the default Python
	pytest --cov=onvif --cov-report html tests/

release: ## package and upload a release
	python3 -m twine upload dist/*

dist: clean ## builds source and wheel package
	python3 setup.py sdist bdist_wheel
	ls -l dist

install: clean ## install the package to the active Python's site-packages
	pip3 install -r requirements.txt
	pre-commit install
	pip3 install -e .
