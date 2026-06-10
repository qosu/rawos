VENV   := /root/rawos/venv
PYTHON := $(VENV)/bin/python3
PYTEST := $(PYTHON) -m pytest
IGNORE := --ignore=tests/setup_load_test.py --ignore=tests/cleanup_load_test.py --ignore=tests/locustfile.py

.PHONY: test test-fast

test:
	$(PYTEST) tests/ $(IGNORE) -q

test-fast:
	$(PYTEST) tests/test_self_probe.py tests/test_tier_enforcement.py tests/test_models.py -q
