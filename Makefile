
venv:
ifndef VIRTUAL_ENV
	$(error Please install and activate a virtualenv before using the init or dev targets)
endif

init: venv
	pip install wheel
	pip install nose

dev: init
	pip install --upgrade -e .
