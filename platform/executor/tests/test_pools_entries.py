"""Pool target switch (local ini, real filesystem) and host parsing. K8sExecSwitch runs against
the clusters (deploy/cloud); here only its parameter validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from bravo_executor.entry import EntrySpec
from bravo_executor.pools import K8sExecSwitch, LocalIniSwitch, SwitchError, host_of

SRC = "host=src-db port=5432 dbname=chat"
DST = "host=dst-db port=5432 dbname=chat"


def test_host_of() -> None:
    assert host_of(SRC) == "src-db"
    assert host_of("dbname=chat host=host.lima.internal port=15434") == "host.lima.internal"
    assert host_of(None) is None and host_of("port=1") is None


async def test_local_switch_reads_and_flips_the_real_file(tmp_path: Path) -> None:
    ini = tmp_path / "pgbouncer.ini"
    ini.write_text(f"[databases]\nchat = {SRC}\n\n[pgbouncer]\nlisten_port = 6432\n", encoding="utf-8")
    sw = LocalIniSwitch(path=ini, db_name="chat", targets={"src": SRC, "dst": DST})
    assert await sw.current_side() == "src"
    assert host_of(await sw.target_line("dst")) == "dst-db"
    evidence = await sw.activate("dst")
    assert evidence == {"from": SRC, "to": DST}
    assert await sw.current_side() == "dst"
    ini.write_text("[databases]\nchat = host=elsewhere\n", encoding="utf-8")
    assert await sw.current_side() is None  # unknown content is not mistaken for a side


def test_k8s_switch_rejects_shell_metacharacters() -> None:
    with pytest.raises(SwitchError):
        K8sExecSwitch(context="bravo-source", namespace="chat", workload="deploy/x; rm -rf /", db_name="chat")
    sw = K8sExecSwitch(context="bravo-onprem", namespace="chat", workload="deploy/chat-pgbouncer", db_name="chat")
    assert sw.describe()["active_path"] == "/run/pgbouncer/active.ini"


def test_entry_repr_hides_the_token() -> None:
    e = EntrySpec(name="aws", side="src", url="http://127.0.0.1:18081", token="s3cr3t-token")  # noqa: S106 - test value
    assert "s3cr3t" not in repr(e) and e.resource == "entry/aws/write-block"


async def test_k8s_switch_records_the_durable_target(tmp_path: Path) -> None:
    """A switch also patches ConfigMap chat-pgbouncer-target, so a PgBouncer restart selects the same side."""
    state = tmp_path / "cm-target"
    state.write_text("src", encoding="utf-8")
    log = tmp_path / "calls"
    fake = tmp_path / "kubectl"
    fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$*" in
  *" exec "*) printf 'chat-pgbouncer-x\\nabc123  /run/pgbouncer/active.ini\\n' ;;
  *" patch configmap chat-pgbouncer-target "*)
    echo "$*" | sed -E 's/.*"target": ?"([a-z]+)".*/\\1/' | tr -d '\\n' > {state} ;;
  *" get configmap chat-pgbouncer-target "*) cat {state} ;;
  *) exit 3 ;;
esac
""", encoding="utf-8")
    fake.chmod(0o755)
    sw = K8sExecSwitch(context="bravo-onprem", namespace="chat", workload="pod/chat-pgbouncer-x", db_name="chat",
                       kubectl=str(fake))
    assert await sw.durable_side() == "src"
    out = await sw.activate("dst")
    assert out["durable_target"] == "dst" and state.read_text(encoding="utf-8") == "dst"
    calls = log.read_text(encoding="utf-8")
    assert "patch configmap chat-pgbouncer-target --type merge -p" in calls
