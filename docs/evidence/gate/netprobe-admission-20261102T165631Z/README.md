# NET-PATHS 시험 Pod 입장 정책 (2026-11-03 01:56–01:57 KST)

게이트 신원의 `chat` Pod 만들기 권한을 ValidatingAdmissionPolicy `bravo-gate-netprobe` + 바인딩으로 좁혔습니다. 세 클러스터 모두 같은 정책이고, 신원 조건과 앱 이미지만 다릅니다(`deploy/platform/link/gate-rbac/{source,onprem,gke}.yaml`). 추가 구성 요소는 없습니다(Kubernetes 1.36 기본 기능).

| 클러스터 | 맞추는 신원(matchConditions) | 허용 이미지(digest 고정) |
|---|---|---|
| 출발지 EKS | `'bravo-gate' in request.userInfo.groups`(access entry 그룹) | `…/bravo-source/bravo-demo@sha256:b5c0e764…` |
| 온프렘 RKE2 | `system:serviceaccount:kube-system:bravo-gate` | `bravo.local/bravo-demo@sha256:b5c0e764…` |
| GKE | `bravo-gate-ro@b-free-target-20261102.iam.gserviceaccount.com`(WIF) | `…/bravo/bravo-demo@sha256:df08e969…` |

조건: 이미지가 앱 Deployment의 이미지와 같음, init 컨테이너 없음, `automountServiceAccountToken: false`, ServiceAccount는 `default`만(앱의 워크로드 신원 금지: EKS IRSA는 토큰 자동 마운트와 무관하게 주입되므로), hostPath · hostNetwork · hostPID · hostIPC · privileged 없음, `activeDeadlineSeconds` ≤ 90, 라벨 `bravo.io/probe=gate-net-paths`. `failurePolicy: Fail`, 동작 `Deny`.

## 시험

`admission-test.sh` → `admission-test.txt`. 운영자가 각 클러스터의 게이트 신원으로 가장(`kubectl --as`)해 `--dry-run=server`로 만들기를 보냈습니다(입장 검사는 하고 저장은 안 함).

- 앱 이미지 · 규칙대로: 세 클러스터 모두 허용.
- 다른 이미지, 토큰 자동 마운트, 기한 300초, 라벨 없음: 세 클러스터 모두 거부(정책 메시지 그대로).
- 정책은 게이트 신원에만 걸립니다: 운영자 신원의 같은 요청은 정책에 맞지 않아 통과(범위 확인).

실제 NET-PATHS: 정책을 건 뒤 Deployment `gate`로 두 쌍을 다시 돌렸습니다(`../platform-gate-20261102T165646Z/`). 세 클러스터 모두 시험 Pod가 만들어지고 측정되고 지워졌습니다(`cleanup: deleted`). 출발지 FAIL `denied_path_reachable`, 온프렘 · GKE PASS로 정책 전(`../platform-gate-20261102T165445Z/`)과 같습니다. UNKNOWN 0.

## 앱 이미지가 바뀌면

정책은 고정한 digest만 허용합니다. 앱 이미지가 바뀌면 시험 Pod가 거부되고 NET-PATHS가 UNKNOWN(`probe_pod_failed`)이 됩니다(닫힌 쪽으로 실패). 갱신 방법:

1. `deploy/platform/link/gate-rbac/<env>.yaml`의 정책에서 이미지 문자열 두 곳(주석 `bravo.io/app-image`, 첫 검증식)을 새 digest로 바꿉니다.
2. `deploy/platform/scripts/gate-credentials.sh rbac`로 적용합니다. 이 단계는 고정값과 실제 `chat-bravo-demo` 이미지가 다르면 경고를 냅니다.
