# 데모 앱 0.3.3 반영 (2026-11-03 03:40–03:46 KST)

변경: `GET /api/rooms/<방>/messages?after=<id>&limit=<n>`(커밋 `7a59dfb`, `a4636ac` 위). WebSocket 이력 읽기(`/ws?room=&after=`)와 같은 행 · 순서 · 상한을 HTTP로 돌려줍니다. 읽기마다 WebSocket을 새로 열지 않게 하려는 것입니다(부하 측정 `docs/evidence/load/`).

## 이미지(한 번 빌드, 세 환경 같은 digest)

`sha256:76e8f595d90267b3d740fdf9012cdf5c93d6337faf63e3601a1da3b2a65c262d`(태그 `0.3.3`, linux/arm64 OCI index). `docker buildx build --platform linux/arm64 --load`로 한 번 빌드했고, 로컬 이미지 Id가 이 값과 같습니다.

| 환경 | 넣은 길 | 확인 |
|---|---|---|
| 출발지 EKS | ECR `bravo-source/bravo-demo:0.3.3`에 push | ECR digest = 위 값. `helm upgrade chat --reuse-values --set image.digest=…`(바뀐 것은 이미지 한 줄, `helm template` 비교). `https://b-free-chat-src…/api/rooms/consult-1/messages` 200, `x-served-by: aws` |
| 온프렘 RKE2 | `docker save` → 두 노드 `ctr images import`(stdin) → `bravo.local/bravo-demo@<digest>`로 태그 | 두 노드 `ctr images ls`에 같은 digest. 같은 helm 방식. `…-dst…` 200, `onprem` |
| GKE | `skopeo copy --preserve-digests --multi-arch all` ECR → Artifact Registry | 복사 뒤 manifest sha256이 위 값과 같음. 같은 helm 방식. `…-gke…` 200, `gcp` |

`deploy/landing-zone/versions.env`와 `overlays/eks/install.sh`의 고정값은 이 갈래가 고치지 않았습니다. 0.3.2 고정값을 고치는 다른 갈래 작업이 커밋되지 않은 채 열려 있었기 때문입니다. 그 갈래가 0.3.3 digest로 커밋합니다(조정자 경유).

## 게이트 NET-PATHS(admission 고정값 갱신 뒤)

`deploy/platform/link/gate-rbac/{source,onprem,gke}.yaml`의 앱 이미지 고정값을 0.3.0(`b5c0e764…`)에서 0.3.3으로 바꾸고 `deploy/platform/scripts/gate-credentials.sh rbac`으로 적용했습니다. 고정값과 실행 중 이미지가 다르다는 경고는 나오지 않았습니다. 0.3.2가 들어간 03:27부터 이 갱신 전까지는 고정값이 실행 이미지와 달라서 시험 Pod가 거부되는 상태였습니다(스크립트 주석의 fail closed).

플랫폼 게이트 `POST /v1/controls/report`(쌍마다 1회, 쓰기 시험 포함):

| 쌍 | 시각(KST) | NET-PATHS | 시험 Pod 이미지 | 게이트 전체 |
|---|---|---|---|---|
| source-aws → target-onprem | 03:43:43–03:44:22 | 출발지 PASS, 목적지 PASS(행동 시험) | `…bravo-demo@sha256:76e8f595…` | BLOCK(`IAM-01`) |
| source-aws → target-gcp | 03:44:22–03:45:02 | 출발지 PASS, 목적지 PASS(행동 시험) | 같음 | BLOCK(`ENC-01`) |

전체 BLOCK은 다른 통제 때문이고 이 변경과 관계없습니다. 원본: `gate-report-source-aws-*.json`, 시각: `gate-run.txt`.
