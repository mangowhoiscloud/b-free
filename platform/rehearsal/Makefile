PY ?= python3
TASK ?= tasks/templates/fix-secret-injection
T ?= tests

.PHONY: setup check test oracle rehearse

setup:
	$(PY) -m pip install -e '.[dev]'

check:
	$(PY) scripts/check_docs.py
	$(PY) scripts/check_tasks.py
	$(PY) scripts/check_notes.py
	$(PY) -m pytest -q

test:
	$(PY) -m pytest -q $(T)

# 정답 해(solution)로 task와 검증기를 먼저 확인합니다. Harbor 0.25.0이 필요합니다.
oracle:
	harbor run -p $(TASK) -a oracle -e docker

rehearse:
	$(PY) -m bravo_rehearsal.runner --task $(TASK)
