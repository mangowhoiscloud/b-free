#!/usr/bin/env bash
# 분리 검증기에서 실행합니다. 판정은 verify.py가 하고, 에이전트 코드는 이 셸이나 검증 프로세스에 로드하지 않습니다.
set -uo pipefail
exec python3 -I "$(dirname "$0")/verify.py" --app /app --reward /logs/verifier/reward.txt
