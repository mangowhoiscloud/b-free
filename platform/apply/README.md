# platform/apply

T31 승인 반영 경로의 플랫폼 쪽 신뢰된 파이프라인입니다(스펙 r2 T31, §8.2 REHEARSE 행, `deploy/CLOUD-E2E-CONTRACT.md` 8절). 결정적 코드만 있고 LLM을 import하지 않습니다(NFR-5). 코드와 설정 파일에 비밀이 없습니다. 서명 토큰은 `keys.sh with-signer`가 cosign의 환경에만 넣습니다.

## 단계

| 단계 | 하는 일 | 거부 · 실패 이유 코드 |
|---|---|---|
| prepare | `tools.verify` → `bound.verify` → `source.materialize`(git 커밋에서, 작업 트리 아님) → `diff.apply` → `image.build`(buildx, OCI, `rewrite-timestamp`) → `sbom.syft` → `scan.trivy` | `tool_pin_mismatch`, `bound_value_mismatch`, `diff_path_rejected`, `diff_files_mismatch`, `scan_blocked` |
| apply | `tools.verify` → `approval.verify` → `image.locate` → `destination.connect` → `policies.scope` → `staging.push` → `image.sign` → `signature.verify` → `destination.import` → `deploy.precheck` → `deploy.rollout` → `deploy.verify` → `policies.apply` → `gate.recheck` | `approved_digest_mismatch`, `approved_diff_mismatch`, `approved_policy_diff_mismatch`, `image_missing`, `oci_manifest_hash_mismatch`, `kube_context_missing`, `onprem_unreachable`, `policy_diff_mismatch`, `policy_out_of_scope`, `policy_target_not_connected:<type>`, `signature_invalid`, `destination_digest_mismatch`, `rollout_failed`, `deployed_digest_mismatch` |

- **승인은 플랫폼 DB에서만 읽습니다**(`src/bravo_apply/approvals.py`). `approval.verify`는 `approvals` 행이 이 변경의 `change` 승인이고, 변경이 `approved` · `applying`이고, 승인자 ≠ 요청자 · 작성자이고, 만료 전일 때만 통과합니다. 파일이나 인자로 받은 승인은 없습니다. CLI `apply`도 `--approval-id`와 `BRAVO_APPLY_DATABASE_URL`이 필요하고, 바인딩 파일에 `approval` 키가 있으면 거부합니다. 2026-11-02 온프렘에 `approval None`으로 배포된 경로(옛 CLI)를 이렇게 막았습니다. 거부 이유: `approval_source_unavailable`, `approval_missing`, `approval_not_found`, `approval_stage_mismatch`, `approval_change_mismatch`, `approval_not_granted`, `approver_is_requester`, `approval_expired`.
- 배포는 모든 노드의 `<repo>@<digest>`가 승인 digest일 때만 하고, 새 Pod가 모두 그 digest로 Ready일 때만 받아들입니다. 아니면 `kubectl rollout undo`.
- apply는 다시 빌드하지 않습니다. 데모 이미지 빌드는 비트 단위로 재현되지 않습니다(2026-11-02 측정: `--no-cache` 두 번, 마지막 층 `/app/.venv` 바이트코드만 다름). 그래서 prepare가 만든 내용 주소 OCI 아카이브(`~/.cache/bravo-apply/images/<hex>.oci.tar`)를 쓸 때마다 다시 해시합니다.
- 서명: `cosign sign --key hashivault://bravo-supply-chain --signing-config signing-config.json`. 서명 설정에 Fulcio · Rekor · TSA가 없어 공개 로그로 나가지 않습니다. 검증은 `keys/bravo-supply-chain.pub`(비밀 아님)만 씁니다.
- 스테이징 레지스트리: `127.0.0.1:5055`의 CNCF distribution 3.1.2(digest 고정) 컨테이너 `bravo-apply-staging`. cosign이 레지스트리 객체에 서명하기 때문에 둡니다. 목적지가 아닙니다.

## 실행

```bash
cd platform/apply && uv sync --frozen
uv run --frozen python -m bravo_apply tools-verify
uv run --frozen python -m bravo_apply prepare --binding B.json --out DIR
BRAVO_APPLY_DATABASE_URL=... uv run --frozen python -m bravo_apply apply --binding B.json --prepared DIR/prepare.json \
  --approval-id <콘솔이 기록한 change 승인 ID> --out DIR2
BRAVO_APPLY_DATABASE_URL=... uv run --frozen python -m bravo_apply worker      # DB 모드(역할 bravo_apply)
uv run --frozen pytest -q -rs
```

| 변수 | 뜻 |
|---|---|
| `BRAVO_APPLY_DATABASE_URL` | 플랫폼 DB, 역할 `bravo_apply`(worker만) |
| `BRAVO_APPLY_GATE_URL` | `platform/gate` 주소. 없으면 `gate.recheck`는 `NOT_CONNECTED`(`gate_unconfigured`) |
| `BRAVO_APPLY_CONFIG`, `BRAVO_APPLY_TOOLS_LOCK` | 기본은 이 폴더의 `config.json`, `tools.lock.json` |
| `BRAVO_APPLY_REPO_ROOT`, `BRAVO_APPLY_CACHE_DIR` | 기본 저장소 루트, `~/.cache/bravo-apply` |

서명 키 준비(한 번): `deploy/pilot/scripts/keys.sh signer-init`, 공개 키: `keys.sh signer-pubkey keys/bravo-supply-chain.pub`.

## 연결 안 된 것

- 목적지 `target-gcp`(GKE): kubectl 컨텍스트가 없어 `NOT_CONNECTED`(`kube_context_missing`). 컨텍스트가 생겨도 Artifact Registry 푸시 자격이 없어 `gke_driver_not_wired`.
- 정책 대상 `rgw_bucket_policy`, `openbao_policy`, `s3_bucket_policy`, `gcs_iam_policy`: 이 파이프라인에 적용 수단이 없어 `policy_target_not_connected:<type>`로 실패(아무것도 적용하지 않음).
- GHCR digest를 끌어오는 경로(`.github/workflows/t31-apply.yaml`이 만든 후보): 비공개 패키지 읽기 자격이 없어 연결 안 됨. 지금은 같은 고정 도구로 로컬 빌드.
- 온프렘은 2026-11-02 저녁 K3s에서 RKE2로 다시 세워졌습니다. 노드 containerd 명령은 `config.json`의 `ctr`(RKE2: `/var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock`)이고, 새 클러스터에서는 아직 실행하지 않았습니다.
- 실행 주체: 지금은 운영자 노트북(custody 파일, Lima, kubectl). 플랫폼 EKS로 옮기면 서명 토큰은 custody 대신 그 Pod의 AppRole 로그인으로 받아야 하고, 온프렘 K3s에는 링크(D-16)로 닿아야 합니다.
