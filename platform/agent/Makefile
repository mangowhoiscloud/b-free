PY ?= python3

.PHONY: test images audit smoke

# 단위 시험(SDK 호출 없음). Harbor 모델 검증은 harbor가 설치된 환경에서만 돎
test:
	$(PY) -m pytest -q -p no:cacheprovider

# 샌드박스 이미지 빌드와 감사(바이너리 기준선, uid, 패키지 관리자 · setuid 없음)
images:
	images/build.sh

audit:
	images/audit.sh

# 허용 목록의 검사 argv를 실제 이미지에서 오프라인으로 실행
smoke:
	$(PY) images/smoke_checks.py
