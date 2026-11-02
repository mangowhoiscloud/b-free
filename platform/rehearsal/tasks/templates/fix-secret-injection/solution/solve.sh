#!/usr/bin/env bash
# 정답 해: 주입된 파일에서 비밀을 읽도록 바꿉니다.
set -euo pipefail
cat > /app/config.py <<'PY'
"""목적지 방식: ESO가 주입한 파일에서 비밀을 읽습니다."""
import os
from pathlib import Path


def db_password() -> str:
    path = Path(os.environ.get("DB_SECRET_PATH", "/var/run/secrets/db/password"))
    return path.read_text().strip()
PY
