"""허용 목록의 검사 argv를 실제 샌드박스 이미지에서 돌려 봅니다(--network none, 읽기 전용 루트, uid 65532).

사용: python3 images/smoke_checks.py   (images/build.sh 뒤. 이미지 태그는 <sandbox.image>:dev)
기대 종료 코드와 다르면 실패로 끝납니다. hadolint는 시험용 Dockerfile의 경고로 1을 냅니다.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from bravo_agents.allowlist import load_allowlist  # noqa: E402

CASES = [  # (역할, 검사, 인자, 기대 종료 코드)
    ("governance", "json_syntax", {"path": "/out/values.json"}, 0),
    ("governance", "k8s_schema", {"path": "/out/rolebinding.yaml"}, 0),
    ("db", "json_syntax", {"path": "/out/values.json"}, 0),
    ("discovery", "search", {"pattern": "secretsmanager"}, 0),
    ("report", "json_extract", {"filter": ".role", "path": "/src/values.json"}, 0),
    ("app_migration", "compile", {}, 0),
    ("app_migration", "make_diff", {}, 0),
    ("ci", "unit_tests", {}, 0),
    ("ci", "compile", {}, 0),
    ("ci", "values_render", {"path": "/out/values.yaml"}, 0),
    ("ci", "manifest_schema", {"path": "/out/rolebinding.yaml"}, 0),
    ("ci", "misconfig_scan", {"path": "/out"}, 0),
    ("ci", "dockerfile_lint", {"path": "/app/Dockerfile"}, 1),
    ("ci", "sbom", {}, 0),
]


def fixtures(d: Path) -> None:
    shutil.copy(HERE / "testdata" / "k8s" / "rolebinding.yaml", d / "rolebinding.yaml")
    (d / "values.json").write_text('{"role": "x"}')
    (d / "values.yaml").write_text("port: 8080\nreplicas: 2\nresources:\n  requests: {cpu: 100m, memory: 128Mi}\n"
                                   "  limits: {cpu: 500m, memory: 256Mi}\nprobes: {readinessPath: /healthz, "
                                   "livenessPath: /healthz}\n")
    (d / "Dockerfile").write_text('FROM python:3.12-slim\nRUN pip install flask\nCMD ["python", "app.py"]\n')
    (d / "app.py").write_text('import boto3\nc = boto3.client("secretsmanager")\n')
    (d / "tests").mkdir()
    (d / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        fx = Path(tmp)
        fixtures(fx)
        for role, name, args, want in CASES:
            al = load_allowlist(role)
            chk = al.sandbox.checks[name]
            env = [x for k, v in chk.env.items() for x in ("-e", f"{k}={v}")]
            cmd = ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", f"{al.sandbox.user}:{al.sandbox.user}",
                   "--tmpfs", f"/work:uid={al.sandbox.user},gid={al.sandbox.user}",
                   "-v", f"{fx}:/src:ro", "-v", f"{fx}:/app:ro", "-v", f"{fx}:/out:ro", "-w", chk.cwd, *env,
                   f"{al.sandbox.image}:dev", *al.check_argv(name, args)]
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=chk.timeout_s + 30).returncode
            ok = rc == want
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {role:14} {name:16} exit={rc} want={want}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
