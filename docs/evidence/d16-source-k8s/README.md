# 출발지 EKS API를 D-16 링크로 읽기(2026-11-02)

플랫폼이 출발지 EKS API에 닿는 길을 바꿨습니다. 전에는 플랫폼 NAT IP를 EKS 공개 엔드포인트 허용 목록에 넣은 임시 링크(D-16 전)였습니다. 지금은 D-16 링크 `t1-src-k8s`(플랫폼 Service `src-k8s.bravo-link.svc:443`)입니다. 시각은 KST입니다.

## 바꾼 것

| 항목 | 내용 |
|---|---|
| 수집기 코드 | 레지스트리 `kubernetes.api_server`를 받으면 그 주소로 연결합니다. TLS는 EKS CA로 확인하고, 이름은 `eks:DescribeCluster`의 실제 엔드포인트 호스트입니다(`tls_server_name`으로 바꿀 수 있음). 확인을 끄는 설정은 없습니다. 스냅숏 경로(`kubernetes.py`)도 같은 EKS 경로를 씁니다. 커밋 `dceb782` |
| 수집기 이미지 | `bravo-platform/collector@sha256:8de337b9f53bd5c9ccef050ea3fc431d55ca539948255164a756b7b56326667c`(태그 `d16link-20261102T143435Z`, linux/arm64). Argo CD가 23:42에 배포 |
| 레지스트리 | `deploy/platform/control/config/environments.yaml`의 `source-aws`에 `api_server: https://src-k8s.bravo-link.svc.cluster.local:443` |
| 인증 | 바꾸지 않았습니다. 웹 아이덴티티 역할 `bravo-platform-collector-ro-…` + access entry 그룹 `bravo-collector` |
| 허용 목록 | `infra/envs/source-aws` `bootstrap_public_cidrs`에서 `43.200.202.252/32`(플랫폼 NAT)를 뺐습니다. EKS 갱신 23:44:01 Successful. 남은 것은 운영자 노트북 IP 하나 |

## 확인 결과

| 시점 | 콘솔 클러스터 데이터 `GET /v1/clusters`(source-aws) | 스냅숏 `kubernetes` | 게이트(출발지 K8s 질문 3개) | 파일 |
|---|---|---|---|---|
| 23:38 바꾸기 전(수집기 `9dc004db`, NAT 길) | ok, `v1.36.5-eks-577a51e`, 노드 1 Ready, 워크로드 23, Pod 30 Running | 오류: kubeconfig 없음 | UNKNOWN `*_not_configured` | `02-before.txt` |
| 23:42 바꾼 뒤(NAT IP는 아직 허용) | 같음 | `server_version` · `namespaces` ok, 네임스페이스별 읽기는 403 | 같음 | `03-after-switch.txt`, `04-console-clusters-after-switch.png` |
| 23:45 NAT IP를 뺀 뒤 | 같음 | 같음 | 같음 | `05-after-nat-removal.txt`, `07-console-clusters-after-nat-removal.png` |

- 링크 확인(`01`, `08`): 수집기 Pod가 자기 신원으로 `src-k8s`에 TLS 1.3으로 붙습니다. 인증서 SAN에 EKS 엔드포인트 호스트가 있습니다. 틀린 이름(`wrong.example.com`)은 `Hostname mismatch`로 거부됩니다. `/version` 응답 101 · 619 · 423 ms(3회).
- 옛 길이 닫힌 것(`08`): NAT IP를 뺀 뒤 같은 수집기 Pod에서 EKS 공개 엔드포인트(13.209.107.84, 15.164.33.52)로 TCP 443이 시간 초과입니다(16초). 같은 분에 링크로는 읽힙니다. 그래서 콘솔 데이터는 링크로만 옵니다.
- Executor(`06`): `src-k8s` + `tls-server-name: kubernetes.default.svc`로 NAT IP를 뺀 뒤에도 출발지 API를 읽습니다(`chat-pgbouncer` Pod 조회).
- 인벤토리 `GET /v1/inventory`는 저장소 정적 분석이라 K8s 길과 무관합니다. 전후 모두 파일 40개, 규칙 `inventory-rules/2026-11-02.1`.
- Terraform(`09`): 바꾸기 전 계획 "No changes". 전체 계획은 애드온 6개를 "known after apply"로 보여 적용하지 않았습니다. 클러스터 자원 하나만 대상으로 계획해 CIDR 한 줄만 바뀌는 것을 보고 그 계획 파일을 적용했습니다. 적용 뒤 전체 계획은 "No changes"입니다.

## 남은 것

- **게이트: UNKNOWN(바꿀 길이 없었음).** 플랫폼 게이트 Deployment에는 클러스터 자격이 없고, 레지스트리 `source-aws`에 게이트 키(`app_namespace`, `app_secret`, `restore_cluster`, `kubeconfig_env`)가 없습니다. 그래서 게이트는 출발지 K8s API를 읽지 않습니다. 출발지 질문 SEC-SECRET-READ-K8S, NET-DEFAULT-DENY, DR-RESTORE는 링크 전후 모두 UNKNOWN입니다. `deploy/environments/real-targets.yaml`은 노트북 E2E용입니다(kubectl 컨텍스트 `bravo-source`, 운영자 IP 길). 게이트가 출발지를 읽으려면 게이트 신원(IAM 역할, access entry, SubjectAccessReview · 메타데이터 읽기 RBAC)이 있어야 합니다. 이것은 설계 결정입니다(제안).
- **스냅숏의 네임스페이스별 읽기는 403입니다.** `bravo-collector` ClusterRole이 클러스터 화면 읽기만 줍니다(`deploy/platform/link/source-rbac/collector.yaml`). 의도한 범위라 넓히지 않았습니다.
- **Executor의 TLS 이름은 `kubernetes.default.svc`로 둡니다.** EKS 인증서 SAN에 있어 확인은 됩니다. 바꾸면 `executor-0`이 다시 뜨므로 데이터 갈래와 맞춘 뒤 바꿉니다.

## 다시 돌리는 법

- `kubectl --context bravo-platform-rke2 -n bravo-control port-forward svc/console 18080:8080` 뒤 `check.sh <이름>`(`summ.py` 사용).
- `kubectl --context bravo-platform-rke2 -n bravo-control exec -i <수집기 Pod> -c collector -- python - < linkprobe.py`(링크), `< natprobe.py`(옛 길).
- 출력에 토큰은 없습니다. 역할 ARN과 계정 ID는 저장소 매니페스트에 이미 있는 값입니다.
