"""문서 색인 표의 경로와 실제 파일이 맞는지 검사합니다. 실패하면 종료 코드 1."""
import re
import sys
from pathlib import Path

# (색인 파일, 표 안의 경로가 기준으로 삼는 폴더)
ROUTING = [(Path("AGENTS.md"), Path(".")), (Path("docs/README.md"), Path("docs"))]
PATH_IN_CELL = re.compile(r"^\|[^|]*\|\s*`([^`<>*]+)`")


def routing_errors() -> list[str]:
    out = []
    for index, base in ROUTING:
        for line in index.read_text(encoding="utf-8").splitlines():
            m = PATH_IN_CELL.match(line)
            if m and not m.group(1).startswith("make ") and not (base / m.group(1).split()[0]).exists():
                out.append(f"{index}: {m.group(1)} 없음")
    return out


def index_errors(index: Path, row: str, files: set[str]) -> list[str]:
    listed = set(re.findall(row, index.read_text(encoding="utf-8"), re.M))
    diff = sorted(listed ^ files)
    return [f"{index}: 색인과 파일 불일치 {diff}"] if diff else []


def main() -> int:
    errs = routing_errors()
    adrs = {p.name[:4] for p in Path("docs/adr").glob("[0-9]*.md") if not p.name.startswith("0000")}
    errs += index_errors(Path("docs/adr/README.md"), r"^\| (\d{4}) \|", adrs)
    notes = {p.stem for p in Path("docs/notes").glob("C-*.md")}
    errs += index_errors(Path("docs/notes/README.md"), r"^\| (C-[A-Z]+-\d{3}) \|", notes)
    for e in errs:
        print(e)
    print(f"check_docs: {len(errs)}건")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
