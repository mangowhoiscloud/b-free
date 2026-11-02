"""blueprint 노트 검사: ID와 파일 이름, 없는 의존, 순환 의존, 기록 없는 '검증됨'. 실패하면 종료 코드 1."""
import re
import sys
from pathlib import Path

STATUSES = {"미정", "추측", "검증됨", "반증됨", "폐기"}


def parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: frontmatter 없음")
    meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    for key in ("uses", "proof_uses"):
        meta[key] = re.findall(r"C-[A-Z]+-\d{3}", meta.get(key, ""))
    meta["body"] = m.group(2)
    return meta


def errors(notes: dict[str, dict]) -> list[str]:
    out = []
    for nid, n in notes.items():
        if n.get("id") != nid:
            out.append(f"{nid}: id와 파일 이름이 다름")
        if n.get("status") not in STATUSES:
            out.append(f"{nid}: 알 수 없는 상태 {n.get('status')}")
        out += [f"{nid}: 없는 의존 {d}" for d in n["uses"] + n["proof_uses"] if d not in notes]
        if n.get("status") == "검증됨":
            log = n["body"].split("## 기록", 1)[-1]
            if not re.search(r"make |runs/", log):
                out.append(f"{nid}: 검증됨인데 기록에 실행 명령이나 결과 경로가 없음")
    out += [f"순환 의존: {' -> '.join(c)}" for c in cycles(notes)]
    return out


def cycles(notes: dict[str, dict]) -> list[list[str]]:
    found, state = [], {}

    def visit(nid: str, stack: list[str]) -> None:
        state[nid] = "open"
        for d in notes[nid]["uses"]:
            if d not in notes:
                continue
            if state.get(d) == "open":
                found.append(stack[stack.index(d):] + [d])
            elif d not in state:
                visit(d, stack + [d])
        state[nid] = "done"

    for nid in notes:
        if nid not in state:
            visit(nid, [nid])
    return found


def main(folder: Path = Path("docs/notes")) -> int:
    notes = {p.stem: parse(p) for p in sorted(folder.glob("C-*.md"))}
    errs = errors(notes)
    for e in errs:
        print(e)
    print(f"check_notes: {len(errs)}건")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
