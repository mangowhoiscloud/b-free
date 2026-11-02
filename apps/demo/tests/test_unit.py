"""Pure logic: poll cursor (late commits, permanent gaps) and configuration rules."""

from __future__ import annotations

import pytest

from bravo_demo import config
from bravo_demo.fanout import PollCursor


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_cursor_delivers_each_id_once_in_order() -> None:
    c = PollCursor(grace_s=2.0, clock=Clock())
    assert c.accept([1, 2, 3]) == [1, 2, 3]
    assert c.accept([2, 3, 4]) == [4]
    assert c.watermark == 4 and not c.gaps


def test_cursor_keeps_gap_open_for_late_commit() -> None:
    clock = Clock()
    c = PollCursor(grace_s=2.0, clock=clock)
    assert c.accept([1, 2, 4]) == [1, 2, 4]
    assert c.open_gaps() == [(3, 3)]
    assert c.watermark == 2
    clock.now = 0.5
    assert c.accept([3, 4]) == [3]  # the late row arrives and is delivered
    assert c.watermark == 4 and not c.gaps


def test_cursor_gives_up_permanent_gap_after_grace() -> None:
    clock = Clock()
    c = PollCursor(grace_s=2.0, clock=clock)
    c.start_from([10])
    assert c.accept([13]) == [13]
    assert c.open_gaps() == [(11, 12)]
    clock.now = 2.5
    assert c.accept([]) == []
    assert c.watermark == 13 and not c.gaps


def test_cursor_handles_sequence_jump_without_enumerating() -> None:
    # The Executor raises the destination sequence at cutover: ids jump by a large step.
    clock = Clock()
    c = PollCursor(grace_s=1.0, clock=clock)
    c.start_from([5])
    assert c.accept([10_000_005]) == [10_000_005]
    assert c.open_gaps() == [(6, 10_000_004)]
    clock.now = 1.5
    c.accept([])
    assert c.watermark == 10_000_005


def test_cursor_start_from_does_not_redeliver_history() -> None:
    c = PollCursor(grace_s=2.0, clock=Clock())
    c.start_from([7, 8, 10])
    assert c.accept([7, 8, 10, 11]) == [11]
    assert c.open_gaps() == [(9, 9)]


BASE = {
    "APP_ENV_ID": "aws",
    "APP_ENV_LABEL": "AWS",
    "DB_DSN": "host=pgbouncer port=6432 dbname=chat user=app_writer",
    "DB_PASSWORD_SOURCE": "env-file",
    "DB_PASSWORD_FILE": "/run/secrets/db",
}


def test_config_minimal() -> None:
    s = config.load(dict(BASE))
    assert s.db.write_wait_s == 8.0 and s.db.statement_timeout_ms == 200
    assert s.s3 is None


@pytest.mark.parametrize(
    "dsn",
    ["host=x dbname=chat user=a password=secret", "postgresql://a:secret@x/chat"],
)
def test_config_rejects_password_in_dsn(dsn: str) -> None:
    with pytest.raises(config.ConfigError, match="must not contain a password"):
        config.load({**BASE, "DB_DSN": dsn})


def test_config_statement_timeout_capped_at_200ms() -> None:
    with pytest.raises(config.ConfigError):
        config.load({**BASE, "DB_STATEMENT_TIMEOUT_MS": "500"})


def test_config_secrets_manager_requires_secret_id() -> None:
    env = {**BASE, "DB_PASSWORD_SOURCE": "aws-secrets-manager"}
    with pytest.raises(config.ConfigError, match="DB_PASSWORD_SECRET_ID"):
        config.load(env)


def test_config_attachment_limit_cannot_exceed_schema_cap() -> None:
    env = {**BASE, "S3_BUCKET": "b", "ATTACHMENT_STORE_ID": "store-src", "ATTACHMENT_MAX_BYTES": str(6 * 1024 * 1024)}
    with pytest.raises(config.ConfigError):
        config.load(env)


def test_config_requires_environment_id() -> None:
    env = dict(BASE)
    del env["APP_ENV_ID"]
    with pytest.raises(config.ConfigError, match="APP_ENV_ID"):
        config.load(env)
    with pytest.raises(config.ConfigError, match="APP_ENV_ID"):
        config.load({**BASE, "APP_ENV_ID": "온프렘"})
