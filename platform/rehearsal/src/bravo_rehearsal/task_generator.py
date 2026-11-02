"""템플릿에서 이전마다 Harbor task 폴더를 만듭니다(docs/architecture/01-task-generator.md)."""
import hashlib
import re
import shutil
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def render(template: Path, out: Path, values: dict[str, str]) -> str:
    """폴더를 만들고 SHA-256을 돌려줍니다. 채우지 못한 자리 표시자가 있으면 실패합니다."""
    if out.exists():
        raise FileExistsError(out)
    shutil.copytree(template, out)
    inst = out / "instruction.md"
    text = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), inst.read_text(encoding="utf-8"))
    left = _PLACEHOLDER.findall(text)
    if left:
        shutil.rmtree(out)
        raise ValueError(f"채우지 못한 자리 표시자: {sorted(set(left))}")
    inst.write_text(text, encoding="utf-8")
    return digest(out)


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(f.relative_to(folder).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()
