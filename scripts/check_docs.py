"""AGENTS.md 색인 경로, 문서 안 상대 링크, ADR 색인이 실제 파일과 맞는지 검사합니다. 실패하면 종료 코드 1."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH_IN_CELL = re.compile(r"^\|[^|]*\|\s*`([^`<>*]+)`")
MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
# ponytail: platform/rehearsal은 자체 check_docs가 있어 건너뜀
SKIP = ("platform/rehearsal/", "docs/spec/spec-")


def routing_errors() -> list[str]:
    out = []
    for line in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines():
        m = PATH_IN_CELL.match(line)
        if m and not (ROOT / m.group(1).split()[0]).exists():
            out.append(f"AGENTS.md: {m.group(1)} 없음")
    return out


def link_errors() -> list[str]:
    out = []
    for md in ROOT.rglob("*.md"):
        rel = md.relative_to(ROOT).as_posix()
        if rel.startswith(SKIP) or "/node_modules/" in rel or "/.venv/" in rel:
            continue
        for target in MD_LINK.findall(md.read_text(encoding="utf-8")):
            if "://" not in target and not (md.parent / target).exists():
                out.append(f"{rel}: 링크 {target} 없음")
    return out


def adr_errors() -> list[str]:
    adr = ROOT / "docs/adr"
    files = {p.name[:4] for p in adr.glob("[0-9]*.md") if not p.name.startswith("0000")}
    listed = set(re.findall(r"^\| (\d{4}) \|", (adr / "README.md").read_text(encoding="utf-8"), re.M))
    diff = sorted(files ^ listed)
    return [f"docs/adr/README.md: 색인과 파일 불일치 {diff}"] if diff else []


def main() -> int:
    errs = routing_errors() + link_errors() + adr_errors()
    for e in errs:
        print(e)
    print(f"check_docs: {len(errs)}건")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
