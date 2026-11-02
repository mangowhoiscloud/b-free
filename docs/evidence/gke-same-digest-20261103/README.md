# GKE 앱을 출발지와 같은 digest로 (2026-11-03 02:51–02:55 KST)

스펙 원칙은 출발지와 목적지가 같은 digest를 받는 것입니다(§8.8). 출발지 EKS와 온프렘은 `bravo-demo@sha256:b5c0e764…`였고, GKE만 따로 빌드한 `df08e969…`(0.3.0)였습니다.

| 순서 | 시각(KST) | 일 | 기록 |
|---|---|---|---|
| 1 | 02:51:45 | `infra/envs/target-gcp/scripts/images.sh`: 출발지 ECR `bravo-source/bravo-demo@sha256:b5c0e764…`를 Artifact Registry `bravo/bravo-demo`로 `skopeo copy --preserve-digests --multi-arch all`. 복사 뒤 목적지 매니페스트 sha256 = `b5c0e764…`. PgBouncer `68c4a15e…`는 이미 있음. 첫 시도는 인증 파이프 하나를 두 번 읽어 복사 전에 실패 | `01-copy.txt` |
| 2 | 02:52:47 | `helm upgrade chat --reuse-values --set-string image.digest=sha256:b5c0e764…`. 서버 dry-run 비교에서 바뀌는 줄은 이미지 하나뿐. 롤아웃 완료, Pod 2개 Ready, 재시작 0 | `02-roll-gke.txt` |
| 3 | 02:53 | 세 클러스터 `chat-bravo-demo` 이미지 digest가 모두 `b5c0e764…`. `https://b-free-chat-gke.mangowhois.ai/` 200, `/readyz` `{"status":"ready","attachments":"ok"}` | `02-roll-gke.txt` |
| 4 | 02:53:20 | 게이트 입장 정책 `bravo-gate-netprobe`(GKE)의 이미지 고정을 `b5c0e764…`로 바꾸고 `gate-credentials.sh rbac`로 적용. 고정값 · 실제 이미지 불일치 경고 없음 | `03-vap-pin.txt` |
| 5 | 02:54–02:55 | 게이트 미리 보기 실행: GKE NET-PATHS PASS, OBJ-PLAIN-EGRESS PASS, 시험 Pod 이미지 `…@sha256:b5c0e764…`, 정리 `deleted` | `../gate/platform-gate-20261102T175426Z/` |

배포 경로: `up.sh` 2단계가 이제 `images.sh`를 부릅니다. 없으면 출발지 레지스트리에서 digest째 복사하고, GKE용 빌드는 없습니다. 자격은 운영자의 AWS · gcloud 체인에서 받아 `/dev/fd` 파이프로 skopeo에 넘깁니다(argv · 디스크에 토큰 없음). 0.3.0 → 0.3.1 사이 앱 차이는 관리 포트 코드(`admin.py`)뿐이고 스키마 변경은 없습니다.
