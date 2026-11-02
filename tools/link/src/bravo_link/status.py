"""JSON status file for a link process, written atomically (tmp + rename). Never holds secrets."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import LINK_LABEL


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class LinkStatus:
    kind: str                       # forward | reverse
    name: str
    label: str = LINK_LABEL
    state: str = "starting"         # starting | up | down | stopped
    started_at: str = field(default_factory=utcnow)
    restarts: int = 0
    last_ok: str | None = None
    last_error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatusWriter:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def write(self, status: LinkStatus) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({**status.as_dict(), "updated_at": utcnow()}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
