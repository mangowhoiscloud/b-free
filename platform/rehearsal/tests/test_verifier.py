import importlib.util
from pathlib import Path

import pytest

TASK = Path(__file__).parent.parent / "tasks/templates/fix-secret-injection"
spec = importlib.util.spec_from_file_location("verify", TASK / "tests/verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

SOLUTION = (TASK / "solution/solve.sh").read_text().split("<<'PY'\n")[1].split("\nPY")[0]
ORIGINAL = (TASK / "environment/app/config.py").read_text()


def run(tmp_path, config: str, reward_before: str | None = None) -> tuple[str, str]:
    app = tmp_path / "app"
    app.mkdir()
    (app / "config.py").write_text(config)
    reward = tmp_path / "logs/reward.txt"
    reward.parent.mkdir()
    if reward_before is not None:
        reward.write_text(reward_before)
    ok, reason, _ = verify.verdict(app)
    reward.write_text("1\n" if ok else "0\n")
    return reward.read_text().strip(), reason


def test_solution_passes(tmp_path):
    assert run(tmp_path, SOLUTION) == ("1", "ok")


def test_solution_with_logs_and_comment_passes(tmp_path):
    # 앱 로그 줄과 "boto3" 주석이 있어도 정답은 PASS
    assert run(tmp_path, "# boto3 의존성을 뺐습니다\nprint('config loaded', flush=True)\n" + SOLUTION) == ("1", "ok")


@pytest.mark.parametrize("config,reason", [
    (ORIGINAL, "wrong_or_missing_output"),                             # no-op 에이전트(boto3 호출은 네트워크 없이 실패)
    ("import sys; sys.exit(0)\n", "wrong_or_missing_output"),        # 리뷰가 재현한 거짓 PASS
    ("def db_password():\n    return 's3cr3t-test'\n", "wrong_or_missing_output"),  # 옛 고정값 맞히기
    ("import sys\nsys.stdout.write('A=x')\nsys.exit(0)\n", "wrong_or_missing_output"),  # 줄바꿈 없는 부분 출력
    # import 때 비밀을 읽어 정답 줄만 찍고 끝냄. 회전 값을 못 맞힘
    ("import os, sys\nprint('A=' + open(os.environ['DB_SECRET_PATH']).read().strip(), flush=True)\nos._exit(0)\n",
     "wrong_or_missing_output"),
    # import 때 한 번 읽고 캐시함. 회전 뒤 옛 값을 돌려줌
    ("import os\n_V = open(os.environ['DB_SECRET_PATH']).read().strip()\ndef db_password():\n    return _V\n",
     "stale_after_rotation"),
])
def test_attacks_fail(tmp_path, config, reason):
    assert run(tmp_path, config, reward_before="1") == ("0", reason)


def test_hang_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "TIMEOUT_SEC", (0.5, 0.5))
    assert run(tmp_path, "import time\ntime.sleep(60)\n") == ("0", "timeout")


def test_symlink_artifact_fails(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "config.py").symlink_to("/etc/hosts")
    assert verify.verdict(app)[:2] == (False, "special_file_in_artifact")


def test_infra_error_leaves_no_reward(tmp_path, monkeypatch):
    def broken(app):
        raise PermissionError("no access")
    reward = tmp_path / "reward.txt"
    reward.write_text("1\n")
    monkeypatch.setattr(verify, "verdict", broken)
    monkeypatch.setattr("sys.argv", ["verify", "--app", str(tmp_path), "--reward", str(reward)])
    assert verify.main() == 2 and not reward.exists()


def test_main_writes_reward_last(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "config.py").write_text("import sys; sys.exit(0)\n")
    reward = tmp_path / "reward.txt"
    reward.write_text("1\n")  # 에이전트가 미리 써 둔 보상
    monkeypatch.setattr("sys.argv", ["verify", "--app", str(app), "--reward", str(reward)])
    verify.main()
    assert reward.read_text() == "0\n"
