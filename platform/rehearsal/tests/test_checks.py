import os
from pathlib import Path

import check_docs
import check_notes
import check_tasks

ROOT = Path(__file__).parent.parent


def test_repo_passes_own_checks():
    os.chdir(ROOT)
    assert check_tasks.main() == 0
    assert check_docs.main() == 0
    assert check_notes.main() == 0


def test_notes_detect_cycle_and_missing():
    notes = {
        "C-A-001": {"id": "C-A-001", "status": "추측", "uses": ["C-A-002"], "proof_uses": [], "body": ""},
        "C-A-002": {"id": "C-A-002", "status": "검증됨", "uses": ["C-A-001", "C-A-009"], "proof_uses": [], "body": "## 기록\n- 없음"},
    }
    errs = check_notes.errors(notes)
    assert any("순환" in e for e in errs)
    assert any("없는 의존 C-A-009" in e for e in errs)
    assert any("검증됨인데" in e for e in errs)
