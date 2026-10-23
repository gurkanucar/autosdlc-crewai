BASE_PYTHON ?= python
ifeq ($(OS),Windows_NT)
VENV_PYTHON := .venv/Scripts/python.exe
else
VENV_PYTHON := .venv/bin/python
endif
PYTHON := $(VENV_PYTHON)
export TEMP := $(CURDIR)/.tmp
export TMP := $(CURDIR)/.tmp
export TMPDIR := $(CURDIR)/.tmp
IDEA ?=
OUTPUT ?=
REQUEST ?=
ARGS ?=

ifneq ($(strip $(IDEA)),)
IDEA_ARG := --idea "$(IDEA)"
endif
ifneq ($(strip $(OUTPUT)),)
OUTPUT_ARG := --output "$(OUTPUT)"
endif
ifneq ($(strip $(REQUEST)),)
REQUEST_ARG := --config "$(REQUEST)"
endif

.PHONY: setup run test

.venv/pyvenv.cfg:
	$(BASE_PYTHON) -c "from pathlib import Path; Path('.tmp').mkdir(exist_ok=True)"
	$(BASE_PYTHON) -m venv .venv

.venv/.autosdlc-ready: .venv/pyvenv.cfg pyproject.toml
	$(BASE_PYTHON) -c "from pathlib import Path; Path('.tmp').mkdir(exist_ok=True)"
	$(VENV_PYTHON) -m pip install -e .
	$(VENV_PYTHON) -m playwright install chromium
	$(VENV_PYTHON) -c "from pathlib import Path; Path('.venv/.autosdlc-ready').touch()"

setup: .venv/.autosdlc-ready

run: .venv/.autosdlc-ready
	$(PYTHON) -m autosdlc run $(REQUEST_ARG) $(IDEA_ARG) $(OUTPUT_ARG) $(ARGS)

test: .venv/.autosdlc-ready
	$(PYTHON) -m pytest -q
