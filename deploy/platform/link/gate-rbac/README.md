# 게이트 읽기 권한(고객 클러스터)

플랫폼 게이트(`platform/gate`)가 고객 클러스터에서 쓰는 자기 읽기 신원의 권한입니다. 수집기 신원과 나눕니다.

| 파일 | 클러스터 | 주체 | 플랫폼에서 오는 길 |
|---|---|---|---|
| `source.yaml` | 출발지 EKS `bravo-source` | 그룹 `bravo-gate`(access entry, 역할 `bravo-platform-gate-ro-*`, `infra/envs/platform-aws/gate.tf`) | D-16 링크 `src-k8s` |
| `onprem.yaml` | 온프렘 RKE2 `bravo-onprem` | SA `kube-system/bravo-gate`(TokenRequest 168h, OpenBao `secret/envs/target-onprem/k8s-gate`) | D-16 링크 `dst-k8s` |
| `gke.yaml` | GKE `bravo-target-gcp` | 사용자 `bravo-gate-ro@b-free-target-20261102.iam.gserviceaccount.com`(WIF 가장, `infra/envs/target-gcp/platform_gate_reader.tf`) | GKE DNS 엔드포인트(IAM `container.clusters.connect`) |

권한은 게이트 질문이 부르는 것뿐입니다.

- SEC-SECRET-READ-K8S: `subjectaccessreviews` create, `namespaces` get. Secret `get`은 주지 않습니다. 그래서 앱 Secret이 있는지는 확인하지 않고(증거 `secret_existence: not_checked_no_get_right`), 네임스페이스가 있는지만 확인합니다.
- NET-DEFAULT-DENY: ns `chat`의 `networkpolicies` list.
- DR-RESTORE: ns `chat`의 `clusters.postgresql.cnpg.io` get.

적용: `deploy/platform/scripts/gate-credentials.sh`가 세 클러스터에 적용하고 자격을 OpenBao에서 플랫폼 Secret으로 넘깁니다.
