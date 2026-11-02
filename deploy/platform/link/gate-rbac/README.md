# 게이트 읽기 권한(고객 클러스터)

플랫폼 게이트(`platform/gate`)가 고객 클러스터에서 쓰는 자기 읽기 신원의 권한입니다. 수집기 신원과 나눕니다.

| 파일 | 클러스터 | 주체 | 플랫폼에서 오는 길 |
|---|---|---|---|
| `source.yaml` | 출발지 EKS `bravo-source` | 그룹 `bravo-gate`(access entry, 역할 `bravo-platform-gate-ro-*`, `infra/envs/platform-aws/gate.tf`) | D-16 링크 `src-k8s` |
| `onprem.yaml` | 온프렘 RKE2 `bravo-onprem` | SA `kube-system/bravo-gate`(TokenRequest 168h, OpenBao `secret/envs/target-onprem/k8s-gate`) | D-16 링크 `dst-k8s` |
| `gke.yaml` | GKE `bravo-target-gcp` | 사용자 `bravo-gate-ro@b-free-target-20261102.iam.gserviceaccount.com`(WIF 가장, `infra/envs/target-gcp/platform_gate_reader.tf`) | D-16 링크 `gke-k8s`(클러스터 CA는 container API, TLS 이름 `kubernetes.default.svc`) |

권한은 게이트 질문이 부르는 것뿐입니다.

- SEC-SECRET-READ-K8S: `subjectaccessreviews` create, `namespaces` get. Secret `get`은 주지 않습니다. 그래서 앱 Secret이 있는지는 확인하지 않고(증거 `secret_existence: not_checked_no_get_right`), 네임스페이스가 있는지만 확인합니다.
- NET-DEFAULT-DENY: ns `chat`의 `networkpolicies` list.
- DR-RESTORE: ns `chat`의 `clusters.postgresql.cnpg.io` get.
- AUD-CONTROL-PLANE · SEC-SECRET-AT-REST(RKE2): `/metrics`(비리소스 URL) get. 온프렘은 kube-apiserver 정적 Pod 하나(`kube-system/kube-apiserver-bravo-onprem-server`)의 get도 있습니다(`--audit-*`, `--encryption-provider-config` 플래그를 증거로).
- NET-PATHS: ns `chat`의 `deployments` get, `pods` create · get · delete, `pods/log` get. 앱 Deployment의 라벨과 이미지로 짧은 시험 Pod를 하나 띄우고 지웁니다(`platform/gate/src/bravo_gate/probes/netpod.py`). Pod 만들기는 RBAC만으로는 아무 이미지 · 아무 사양이 됩니다. 그래서 같은 파일의 ValidatingAdmissionPolicy `bravo-gate-netprobe`(+ 바인딩, 동작 `Deny`, `failurePolicy: Fail`)가 게이트 신원의 `chat` Pod 만들기만 골라 이것만 허용합니다.
  - 이미지 = 앱 Deployment의 이미지(digest 고정, 정책 주석 `bravo.io/app-image`), init 컨테이너 없음
  - `automountServiceAccountToken: false`, ServiceAccount `default`만(앱의 워크로드 신원 금지)
  - hostPath · hostNetwork · hostPID · hostIPC · privileged 없음, `activeDeadlineSeconds` ≤ 90, 라벨 `bravo.io/probe=gate-net-paths`
  - 신원 조건: 출발지 그룹 `bravo-gate`, 온프렘 SA `kube-system/bravo-gate`, GKE `bravo-gate-ro@…`. 다른 신원의 요청에는 걸리지 않습니다.
  - 시험: `docs/evidence/gate/netprobe-admission-20261102T165631Z/`(게이트 신원으로 다른 이미지 · 토큰 마운트 · 긴 기한 · 라벨 없음은 거부, 규칙대로는 허용, 실제 NET-PATHS 동작).

**앱 이미지가 바뀌면.** 정책이 시험 Pod를 거부하고 NET-PATHS는 UNKNOWN(`probe_pod_failed`)이 됩니다(닫힌 쪽으로 실패). 해당 `<env>.yaml` 정책의 이미지 두 곳(주석, 첫 검증식)을 새 digest로 바꾸고 `deploy/platform/scripts/gate-credentials.sh rbac`로 적용합니다. 이 단계는 고정값과 실제 `chat-bravo-demo` 이미지가 다르면 경고합니다.

플랫폼 쪽 OpenBao(온프렘 · GKE 앱 비밀의 원본): 게이트 SA `bravo-control/gate`의 Kubernetes 인증 역할 두 개(`keys.sh gate-bao-setup`). `gate-meta`는 앱 비밀 경로의 KV 메타데이터만 읽고(있는지), `gate-other`는 권한이 없습니다(값 읽기는 403이어야 함). TokenReview용 `system:auth-delegator` 바인딩 `bravo-gate-tokenreview`가 플랫폼에 있습니다.

GKE 수집기: `gke-rbac/collector.yaml`(수집기 읽기 ClusterRole을 Google SA `bravo-cost-reader`에 묶음, 링크 `gke-k8s`).

적용: `deploy/platform/scripts/gate-credentials.sh`가 세 클러스터에 적용하고 자격을 OpenBao에서 플랫폼 Secret으로 넘깁니다.
