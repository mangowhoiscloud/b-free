PY ?= python3

.PHONY: check

check:
	$(PY) scripts/check_docs.py
