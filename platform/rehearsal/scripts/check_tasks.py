"""task 불변 조건 검사(tasks/AGENTS.md). 실패하면 종료 코드 1."""
import sys
import tomllib
from pathlib import Path

REQUIRED_HEADINGS = ["목표", "할 수 있는 것", "할 수 없는 것", "예산", "멈춤"]


def problems(task: Path) -> list[str]:
    out = []
    cfg = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    if cfg.get("verifier", {}).get("environment_mode") != "separate":
        out.append("verifier.environment_mode가 separate가 아님")
    if cfg.get("agent", {}).get("network_mode") != "no-network":
        out.append("agent.network_mode가 no-network가 아님")
    if not (task / "tests").is_dir():
        out.append("tests/ 없음")
    if any("reward" in p.name or p.name == "tests" for p in (task / "environment").rglob("*")):
        out.append("environment/ 안에 테스트나 reward 파일이 있음")
    inst = (task / "instruction.md").read_text(encoding="utf-8")
    out += [f"instruction.md에 '{h}' 절 없음" for h in REQUIRED_HEADINGS if h not in inst]
    return out


def main(root: Path = Path("tasks")) -> int:
    bad = 0
    for toml in sorted(root.rglob("task.toml")):
        for p in problems(toml.parent):
            print(f"{toml.parent}: {p}")
            bad += 1
    print(f"check_tasks: {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
