"""분리 검증기 본체. 에이전트가 고친 코드를 이 프로세스에 import하지 않습니다.

판정은 산출물의 종료 코드가 아니라 바깥에서 관찰한 출력으로 합니다. 검증할 때마다 새 난수 비밀을 만들고,
별도 프로세스로 돌린 앱이 그 값을 돌려줘야 합니다. 첫 값을 받은 뒤 비밀 파일을 새 값으로 바꾸고(회전),
다음 호출이 새 값을 돌려줘야 PASS입니다. import 때 정답 줄만 찍는 위조는 회전 값을 맞히지 못합니다.
reward는 맨 마지막에 이 프로세스만 씁니다.
"""
import argparse
import os
import pwd
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); import config\n"
    "print('A=' + config.db_password(), flush=True)\n"
    "sys.stdin.readline()\n"
    "print('B=' + config.db_password(), flush=True)\n"
)
TIMEOUT_SEC = (10.0, 5.0)  # 첫 호출(import 포함), 회전 뒤 호출


def _drop_privileges():
    nobody = pwd.getpwnam("nobody")
    os.setgroups([])
    os.setgid(nobody.pw_gid)
    os.setuid(nobody.pw_uid)


class _Lines:
    """출력에서 결과 줄을 찾습니다. 앱 로그 줄은 건너뛰고, 한 번에 읽힌 나머지 바이트는 다음 읽기에 남깁니다."""

    def __init__(self, fd: int):
        self.fd, self.buf = fd, b""

    def find(self, prefix: str, timeout: float) -> str | None:
        """prefix로 시작하는 줄을 돌려줍니다. 시간 초과면 None, 프로세스가 먼저 끝나면 ""입니다."""
        end = time.monotonic() + timeout
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                text = line.decode(errors="replace")
                if text.startswith(prefix):
                    return text
            left = end - time.monotonic()
            if left <= 0 or len(self.buf) > 65536 or not select.select([self.fd], [], [], left)[0]:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                return ""
            self.buf += chunk


def _run_probe(copy: Path, secret: Path, root: bool, nonces: tuple[str, str]) -> str:
    """앱을 별도 세션의 프로세스로 돌려 두 번 묻고, 사이에 비밀을 회전합니다. 끝나면 그 프로세스 그룹을 지웁니다.

    그룹을 벗어난 자손은 남을 수 있습니다(같은 UID일 때 남는 위험, 05-agent-rehearsal.md 6절).
    """
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", PROBE, str(copy)],
        env={"DB_SECRET_PATH": str(secret), "PATH": "/usr/bin:/bin"},
        cwd=copy.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, preexec_fn=_drop_privileges if root else None,
    )
    try:
        lines = _Lines(proc.stdout.fileno())
        first = lines.find("A=", TIMEOUT_SEC[0])
        if first is None:
            return "timeout"
        if first != "A=" + nonces[0]:
            return "wrong_or_missing_output"
        secret.write_text(nonces[1] + "\n")
        try:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
        except BrokenPipeError:
            return "wrong_or_missing_output"
        second = lines.find("B=", TIMEOUT_SEC[1])
        if second is None:
            return "timeout"
        if not second:
            return "wrong_or_missing_output"
        return "ok" if second == "B=" + nonces[1] else "stale_after_rotation"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def verdict(app: Path) -> tuple[bool, str, str]:
    """(합격, 사유, 격리 방식)을 돌려줍니다."""
    root = os.geteuid() == 0
    isolation = "nobody" if root else "same_uid"  # ponytail: same_uid면 분리 프로세스가 reward를 덮을 수 있음. 완전한 분리는 T15의 재배포 검증
    entries = list(app.rglob("*"))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in entries):
        return False, "special_file_in_artifact", isolation
    # ponytail: 소스 문자열 검사는 주석만으로도 정답을 떨어뜨려 뺐음. 판정은 실행 관찰로만 함
    work = Path(tempfile.mkdtemp(prefix="verify-", dir="/tmp"))  # nobody가 지나갈 수 있는 위치
    try:
        copy = work / "app"
        shutil.copytree(app, copy)
        nonces = (secrets.token_hex(16), secrets.token_hex(16))
        secret = work / "password"
        secret.write_text(nonces[0] + "\n")
        for p in [work, copy, secret, *copy.rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)
        reason = _run_probe(copy, secret, root, nonces)
        return reason == "ok", reason, isolation
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="/app")
    ap.add_argument("--reward", default="/logs/verifier/reward.txt")
    a = ap.parse_args()
    reward = Path(a.reward)
    reward.parent.mkdir(parents=True, exist_ok=True)
    reward.unlink(missing_ok=True)
    try:
        ok, reason, isolation = verdict(Path(a.app))
    except Exception as e:
        # 검증기 쪽 준비 오류는 해답 실패가 아닙니다. reward를 남기지 않아 러너가 INFRA_ERROR로 분류합니다(verdict.classify).
        print(f"verdict=INFRA_ERROR reason={type(e).__name__}")
        return 2
    reward.write_text("1\n" if ok else "0\n")
    print(f"verdict={'PASS' if ok else 'FAIL'} reason={reason} isolation={isolation}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
