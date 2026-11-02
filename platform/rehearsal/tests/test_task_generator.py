from pathlib import Path

import pytest

from bravo_rehearsal import task_generator

TEMPLATE = Path(__file__).parent.parent / "tasks/templates/fix-secret-injection"


def test_render_fills_and_hashes(tmp_path):
    sha = task_generator.render(TEMPLATE, tmp_path / "t1", {"goal": "g", "snapshot_summary": "s"})
    assert "{{" not in (tmp_path / "t1/instruction.md").read_text(encoding="utf-8")
    assert sha == task_generator.digest(tmp_path / "t1")


def test_render_rejects_missing_value(tmp_path):
    with pytest.raises(ValueError):
        task_generator.render(TEMPLATE, tmp_path / "t2", {"goal": "g"})
    assert not (tmp_path / "t2").exists()
