.DEFAULT_GOAL := run
ifeq ($(OS),Windows_NT)
BASE_PYTHON ?= python
VENV_PYTHON := .venv/Scripts/python.exe
else
# Most macOS/Linux installs only have python3.
BASE_PYTHON ?= python3
VENV_PYTHON := .venv/bin/python
endif
PYTHON := $(VENV_PYTHON)
export PYTHONUTF8 := 1
export TEMP := $(CURDIR)/.tmp
export TMP := $(CURDIR)/.tmp
export TMPDIR := $(CURDIR)/.tmp
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
	$(PYTHON) -m streamlit run streamlit_app.py --server.headless false --server.showEmailPrompt false --browser.gatherUsageStats false --server.address 127.0.0.1 --theme.base light

test: .venv/.autosdlc-ready
	$(PYTHON) -m pytest -q
