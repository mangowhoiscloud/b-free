"""The two phases of a change (migration 0004 contract). Same code for worker (DB) and CLI mode.

prepare: tools.verify -> bound.verify -> source.materialize -> diff.apply -> image.build -> sbom.syft -> scan.trivy
apply:   tools.verify -> approval.verify -> image.locate -> destination.connect -> policies.scope -> staging.push
         -> image.sign -> signature.verify -> destination.import -> deploy.precheck -> deploy.rollout -> deploy.verify
         -> policies.apply -> gate.recheck

No rebuild in apply: the demo image build is not bit-reproducible (measured 2026-11-02: two --no-cache builds with
SOURCE_DATE_EPOCH and rewrite-timestamp differ only in the last layer, /app/.venv bytecode). The approved image
is the content-addressed OCI archive from prepare, re-hashed before every use.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import build, digests, gate, policies, supply, tools
from .config import Config
from .destination import driver
from .runtime import Recorder, Result, Step, Stop, run_stage

IMAGE_ROLES = frozenset({"app_migration", "ci"})


@dataclass
class Change:
    change_id: str
    role: str
    target_env: str
    artifact: dict[str, Any] | None
    bound: dict[str, Any]                     # recorded artifact_digest, diff_digest, policy_diff_digest, policy_targets
    approval: dict[str, Any] | None = None    # approval_id, image_digest, diff_digest, policy_diff_digest
    prepared: dict[str, Any] | None = None    # image_repository, image_digest from prepare
    provenance: str = "platform DB change row"
    outputs: dict[str, Any] = field(default_factory=dict)


def _tools(cfg: Config, names: list[str], phase: str) -> Step:
    checks = tools.verify(tools.load_lock(cfg.lock_path), names)
    bad = [c for c in checks if not c["ok"]]
    detail = {"tools": [{k: c.get(k) for k in ("name", "version", "sha256", "ok", "reason")} for c in checks]}
    if bad:
        raise Stop("prepare_failed" if phase == "prepare" else "failed", "tool_pin_mismatch", Result.REFUSED, detail)
    return Step(detail=detail)


def _path(cfg: Config, name: str) -> str:
    return tools.load_lock(cfg.lock_path)[name].path


def prepare(cfg: Config, ch: Change, rec: Recorder, out: Path) -> dict[str, Any]:
    P = "prepare"
    run_stage(rec, P, "tools.verify", lambda: _tools(cfg, ["docker-buildx", "syft", "trivy"], P))

    def _bound() -> Step:
        bad = digests.mismatches(ch.artifact, ch.bound)
        if bad:
            raise Stop("prepare_failed", "bound_value_mismatch", Result.REFUSED,
                       {"mismatch": bad, "recomputed": digests.bound_values(ch.artifact)})
        return Step(detail={**digests.bound_values(ch.artifact), "artifact": "present" if ch.artifact else "none"})

    run_stage(rec, P, "bound.verify", _bound)
    result: dict[str, Any] = {"image_repository": None, "image_digest": None, "sbom_digest": None, "scan": None, "build": None}
    if ch.role not in IMAGE_ROLES:
        for name in ("source.materialize", "diff.apply", "image.build", "sbom.syft", "scan.trivy"):
            run_stage(rec, P, name, lambda: Step(Result.SKIPPED, {"reason": f"role_{ch.role}_has_no_image"}))
        return result
    dest = cfg.destination(ch.target_env)
    if dest is None:
        run_stage(rec, P, "source.materialize", _raise(Stop("prepare_failed", "destination_unknown", Result.FAIL,
                                                             {"target": ch.target_env})))
    app = cfg.app(dest["app"])  # type: ignore[index]
    work = Path(tempfile.mkdtemp(prefix=f"bravo-apply-{ch.change_id[:8]}-", dir=cfg.cache_dir if cfg.cache_dir.is_dir() else None))
    src = run_stage(rec, P, "source.materialize",
                    lambda: Step(value=build.materialize(cfg.repo_root, app["ref"], app["subdir"], work / "src"))).value
    app_dir = work / "src" / app["subdir"]
    diff = digests.diff_text(ch.artifact)
    if diff is None:
        run_stage(rec, P, "diff.apply", lambda: Step(Result.SKIPPED, {"reason": "no_diff_in_artifact"}))
    else:
        files = (ch.artifact or {}).get("files")
        run_stage(rec, P, "diff.apply", lambda: build.apply_diff(app_dir, diff, files if isinstance(files, list) else None))
    name = f"{dest['image_repository']}:change-{ch.change_id[:12]}"  # type: ignore[index]
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    img = run_stage(rec, P, "image.build", lambda: _with_value(
        build.buildx(app_dir, app["dockerfile"], app["platform"], name, src["source_date_epoch"], cfg.cache_dir, work,
                     _path(cfg, "docker-buildx")), keys=("digest", "layers", "platform", "size_bytes", "name"))).value
    tar = Path(img["archive"])
    sbom = run_stage(rec, P, "sbom.syft", lambda: _with_value(
        build.syft_sbom(_path(cfg, "syft"), tar, out / "sbom.spdx.json"))).value

    def _scan() -> Step:
        s = build.trivy_scan(_path(cfg, "trivy"), tar, out / "trivy.json", cfg.scan_policy)
        detail = {"counts": s["counts"], "policy": s["policy"]["result"], "blocking_total": s["policy"]["blocking_total"],
                  "db": s["db"]}
        if s["policy"]["result"] == "fail":
            raise Stop("prepare_failed", "scan_blocked", Result.REFUSED, {**detail, "blocking": s["policy"]["blocking"]})
        return Step(detail=detail, value=s)

    scan = run_stage(rec, P, "scan.trivy", _scan).value
    lock = tools.load_lock(cfg.lock_path)
    result.update(
        image_repository=dest["image_repository"],  # type: ignore[index]
        image_digest=img["digest"],
        sbom_digest=sbom["sbom_digest"],
        scan=scan,
        build={"builder": "docker buildx (type=oci, rewrite-timestamp=true, provenance/sbom attestations off)",
               "tools": {n: {"version": lock[n].version, "sha256": lock[n].sha256} for n in ("docker-buildx", "syft", "trivy")},
               "base": src, "platform": app["platform"], "archive_name": img["name"], "layers": img["layers"],
               "size_bytes": img["size_bytes"], "sbom_packages": sbom["packages"],
               "reproducible": False,
               "reproducible_note": "two --no-cache builds of apps/demo differ in the last layer (/app/.venv bytecode); "
                                    "apply uses this content-addressed archive instead of rebuilding"},
    )
    (out / "prepare.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def apply(cfg: Config, ch: Change, rec: Recorder, out: Path) -> dict[str, Any]:
    A = "apply"
    run_stage(rec, A, "tools.verify", lambda: _tools(cfg, ["cosign", "skopeo", "kubectl", "limactl"], A))
    approval, prepared = ch.approval or {}, ch.prepared or {}
    approved_image = approval.get("image_digest")

    def _approval() -> Step:
        if not approval:
            raise Stop("refused", "approval_missing", Result.REFUSED)
        problems = []
        if approved_image != prepared.get("image_digest"):
            problems.append("approved_digest_mismatch")
        if approval.get("diff_digest") != digests.diff_digest(ch.artifact):
            problems.append("approved_diff_mismatch")
        if approval.get("policy_diff_digest") != digests.policy_diff_digest(ch.artifact):
            problems.append("approved_policy_diff_mismatch")
        if problems:
            raise Stop("refused", problems[0], Result.REFUSED,
                       {"problems": problems, "approved_image_digest": approved_image,
                        "prepared_image_digest": prepared.get("image_digest")})
        return Step(detail={"approval_id": approval.get("approval_id"), "image_digest": approved_image,
                            "diff_digest": approval.get("diff_digest"), "policy_diff_digest": approval.get("policy_diff_digest"),
                            "provenance": ch.provenance})

    run_stage(rec, A, "approval.verify", _approval)
    tar: Path | None = None
    if approved_image:
        def _locate() -> Step:
            p = build.image_path(cfg.cache_dir, approved_image)
            if not p.is_file():
                raise Stop("failed", "image_missing", Result.FAIL, {"expected": approved_image})
            info = build.oci_manifest_digest(p)
            if info["digest"] != approved_image:
                raise Stop("refused", "image_digest_mismatch", Result.REFUSED, {"expected": approved_image, "archive": info["digest"]})
            return Step(detail={k: info[k] for k in ("digest", "layers", "platform", "size_bytes")}, value=(p, info))

        tar, info = run_stage(rec, A, "image.locate", _locate).value
    else:
        run_stage(rec, A, "image.locate", lambda: Step(Result.SKIPPED, {"reason": "no_image_change"}))

    dest = cfg.destination(ch.target_env)
    if dest is None:
        run_stage(rec, A, "destination.connect", _raise(Stop("failed", "destination_unknown", Result.FAIL, {"target": ch.target_env})))
    lock = tools.load_lock(cfg.lock_path)
    drv = run_stage(rec, A, "destination.connect", lambda: _connect(dest, lock)).value
    objs = run_stage(rec, A, "policies.scope", lambda: _scope(ch, approval, dest)).value  # type: ignore[arg-type]

    signature, deployed = None, None
    if tar is not None and approved_image:
        reg = cfg.registry
        repo = "bravo/" + dest["image_repository"].rsplit("/", 1)[-1]  # type: ignore[index]
        staged = run_stage(rec, A, "staging.push", lambda: _with_value(
            {**supply.ensure_registry(reg), **supply.push(lock["skopeo"].path, tar, reg["address"], repo,
                                                         f"change-{ch.change_id[:12]}", approved_image)})).value
        notes = {"bravo.dev/change-id": ch.change_id, "bravo.dev/approval-id": str(approval.get("approval_id")),
                 "bravo.dev/target": ch.target_env}
        signed = run_stage(rec, A, "image.sign", lambda: _with_value(supply.sign(
            cfg.path(cfg.signer["keys_sh"]), lock["cosign"].path, cfg.signer["key_ref"], staged["ref"], notes,
            cfg.path(cfg.signer["signing_config"])))).value
        verified = run_stage(rec, A, "signature.verify", lambda: _with_value(supply.verify(
            lock["cosign"].path, cfg.path(cfg.signer["public_key"]), staged["ref"], approved_image, notes))).value
        signature = {**signed, "verify": verified, "staging": staged}
        (out / "signature.json").write_text(json.dumps(signature, indent=2))
        run_stage(rec, A, "destination.import", lambda: drv.import_image(tar, approved_image, info["name"]))
        run_stage(rec, A, "deploy.precheck", lambda: drv.precheck(approved_image))
        cause = f"bravo T31 change {ch.change_id} approval {approval.get('approval_id')}"
        run_stage(rec, A, "deploy.rollout", lambda: drv.rollout(approved_image, cause))
        run_stage(rec, A, "deploy.verify", lambda: drv.verify(approved_image))
        deployed = approved_image
    else:
        for name in ("staging.push", "image.sign", "signature.verify", "destination.import", "deploy.precheck",
                     "deploy.rollout", "deploy.verify"):
            run_stage(rec, A, name, lambda: Step(Result.SKIPPED, {"reason": "no_image_change"}))
    applied = run_stage(rec, A, "policies.apply", lambda: policies.apply(drv.k, objs, dest["namespace"]))  # type: ignore[index]
    gate_step = run_stage(rec, A, "gate.recheck", lambda: gate.recheck(cfg.gate_url, dest.get("gate")))  # type: ignore[union-attr]
    result = {"signature": signature, "deployed_digest": deployed,
              "policies_applied": applied.detail if applied.result is Result.OK else {"status": "skipped", **applied.detail},
              "gate_recheck": gate_step.value}
    (out / "apply.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def _connect(dest: dict[str, Any], lock: dict[str, tools.Pin]) -> Step:
    drv = driver(dest, lock["kubectl"].path, lock["limactl"].path)
    return Step(detail=drv.reachable(), value=drv)


def _scope(ch: Change, approval: dict[str, Any], dest: dict[str, Any]) -> Step:
    objs = policies.check_scope(digests.translations(ch.artifact), approval.get("policy_diff_digest"), dest)
    if not objs:
        return Step(Result.SKIPPED, {"reason": "no_policy_diff"}, [])
    return Step(detail={"objects": [f"{o['kind']}/{o['metadata']['name']}" for o in objs]}, value=objs)


def _with_value(value: dict[str, Any], keys: tuple[str, ...] | None = None) -> Step:
    detail = {k: value[k] for k in keys} if keys else {k: v for k, v in value.items() if not isinstance(v, (dict, list))}
    return Step(detail=detail, value=value)


def _raise(stop: Stop):
    def fn() -> Step:
        raise stop
    return fn
