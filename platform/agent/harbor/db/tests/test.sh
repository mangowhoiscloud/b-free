#!/bin/sh
# 분리 검증기에서 실행합니다. 판정은 verify.py가 하고, 에이전트 산출물은 데이터로만 읽습니다.
set -eu
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec python3 -I "$here/verify.py" \
  --proposal /out/proposal.json \
  --expected "$here/expected.json" \
  --reward /logs/verifier/reward.txt \
  --details /logs/verifier/details.json
