# landing zone: gke overlay

GKE Standard + Dataplane V2 목적지(`infra/envs/target-gcp`)에 까는 랜딩 존입니다(D-21, D-22, D-23, D-26). 이름, 판, 계약은 `deploy/CLOUD-E2E-CONTRACT.md`와 같고, 다른 것은 아래 "GKE 어댑터"뿐입니다. 올리고 내리는 명령은 `infra/envs/target-gcp/scripts/`에 있습니다.

```
infra/envs/target-gcp/scripts/up.sh       # Terraform + 랜딩 존 + 앱(멱등)
infra/envs/target-gcp/scripts/verify.sh   # 인프라 · 랜딩 존 · 통제 · OBJ-01..06(GCS)
infra/envs/target-gcp/scripts/down.sh     # 지우기(LB · 디스크 정리 뒤 terraform destroy)
```

## 무엇이 있나

| 파일 | 계약 이름 |
|---|---|
| `manifests/00-namespaces.yaml` | `chat`, `bravo-edge`, `cf-edge`, `bravo-observability`. Pod Security `restricted` |
| `manifests/10-storageclasses.yaml` | `bravo-db`, `bravo-general` |
| `manifests/20-secretstore.yaml`, `21-externalsecrets.yaml` | ESO만. 플랫폼 OpenBao `secret/envs/target-gcp/*`에서 `bravo-demo-db`, `chat-admin`, `chat-pgbouncer-auth`, `chat-db-owner`, `chat-db-roles`, `bravo-demo-gcs-hmac`, `cf-edge/cf-tunnel` |
| `manifests/30-chat-db.yaml` | CNPG `Cluster` `chat-db`, PostgreSQL 16, `wal_level=logical`, `max_replication_slots=10`, `max_slot_wal_keep_size=2GB`, Service `chat-db-rw` |
| `manifests/31-pgbouncer.yaml` | Deployment `chat-pgbouncer`(replica 1), `/etc/pgbouncer/targets/target-{src,dst}.ini`, `%include /run/pgbouncer/active.ini`, 처음 대상 `target-src` |
| `manifests/40-gateway.yaml` | Gateway `bravo-gateway`(ns `bravo-edge`), HTTPRoute `chat` → Service `chat-bravo-demo` |
| `manifests/41-cf-edge.yaml` | ns `cf-edge`, Deployment `cloudflared`(digest), Secret `cf-tunnel`의 `token` |
| `manifests/50-otel-agent.yaml` | DaemonSet `otel-agent`, Service `otel-agent:4317` → 플랫폼 Collector |
| `manifests/60-networkpolicies.yaml` | 기본 차단 + 허용(Dataplane V2가 집행) |
| `values/chat.yaml` | 데모 앱(릴리스 `chat`). 출발지와 같은 차트, 같은 digest |
| `versions.env` | GKE 전용 이름만. 차트 판과 공통 이미지 digest는 `deploy/landing-zone/versions.env`(모든 클러스터 같은 판)를 먼저 읽음 |

`${VAR}` 자리는 `up.sh`가 Terraform 출력으로 채웁니다(`envsubst`, 변수 목록 고정). 채운 결과는 `~/.bravo/target-gcp/rendered/`에 남고 비밀 값은 없습니다.

## GKE 어댑터(다른 overlay와 다른 것만)

| 항목 | GKE에서 | 이유 |
|---|---|---|
| 노드 | C4A(Arm) `c4a-standard-4`, 도쿄 `asia-northeast1-b` | 데모 이미지가 arm64 하나뿐이라 출발지(EKS t4g)와 같은 digest를 띄우려면 Arm 노드가 필요. 서울(asia-northeast3)에는 C4A가 없음(2026-11-02 `machine-types list`) |
| 블록 저장 | PD CSI, `hyperdisk-balanced` + CMEK | C4A에는 Hyperdisk만 붙음. TECH-STACK-SPEC의 `pd-balanced`와 다름 |
| 진입 | GKE Gateway 컨트롤러, 클래스 `gke-l7-rilb`(내부 지역 Application LB, Google 관리 Envoy) | Dataplane V2는 관리형 Cilium(anetd)이라 Cilium의 Gateway API · Envoy를 켤 수 없음. 공개 주소 없이 Cloudflare만 들어옴 |
| 진입 Service 이름 | `bravo-edge/bravo-gateway`(셀렉터 없는 Service + EndpointSlice → Gateway 고정 내부 주소) | 터널 `origin_service`를 다른 환경과 같은 `http://bravo-gateway.bravo-edge.svc.cluster.local:80`로 둠 |
| 관측(Hubble) | Dataplane V2 관측(관리형 Hubble relay) | Cilium 직접 설치 불가 |
| 첨부 저장 | Cloud Storage XML API + HMAC(같은 S3 계약 변수) | D-22 |
| 비밀 | ClusterSecretStore `bravo-platform-openbao`(base와 같은 이름), AppRole `eso-target-gcp`(Secret `external-secrets/eso-target-gcp`, 키 `role_id` · `secret_id`). ESO → 링크 Service `openbao:8200`(ns `external-secrets`) → 플랫폼 OpenBao `openbao/openbao-0`(custody `bao_target`이 정하는 플랫폼 클러스터, 2026-11-02 21:40 KST 기준 `bravo-platform-rke2`) | D-26. 이름 `openbao`가 플랫폼 OpenBao 인증서 SAN과 같아 TLS를 끝까지 검증. base의 `openbao.bravo-link.invalid`(링크 대기)를 GKE에서는 실제 링크로 채움 |
| Arm taint | 노드 풀 `architecture_taint_behavior = NONE` | 모든 노드가 Arm이라 차트마다 toleration을 넣지 않음 |
| DNS 허용 규칙 | `k8s-app=kube-dns`와 `k8s-app=node-local-dns`(kube-system) 둘 다 | GKE는 NodeLocal DNSCache가 기본으로 켜져 있어 Pod DNS를 node-local-dns Pod가 받음. 하나만 열면 DNS가 막힘(2026-11-02 실측) |

## 임시 링크(D-16 전)

`infra/envs/target-gcp/scripts/links.sh`가 `tools/link`(bravo-link)의 relay를 셋 띄우고 노트북 에이전트를 붙입니다.

| relay(ns) | 노트북 쪽 대상 |
|---|---|
| `openbao`(external-secrets) | 플랫폼 클러스터 `openbao/openbao-0:8200`(port-forward, 컨텍스트는 custody `bao_target`: `platform-rke2` → `bravo-platform-rke2`, `platform-eks` → `bravo-platform`) |
| `platform-otel`(bravo-observability) | 같은 플랫폼 클러스터의 `bravo-observe/otel-collector:4317` |
| `src-db-link`(chat) | 플랫폼이 연 출발지 DB 앱 경로 `127.0.0.1:15434`(계약 9절) |

노트북이 꺼지면 링크도 끊깁니다. ESO는 마지막으로 받은 Secret을 유지하고, 새로 고침만 실패합니다.

## base와 맞출 것

이 overlay는 `base/`가 생기기 전에 혼자 서도록 썼습니다. 지금 맞춘 것: 공통 판(`deploy/landing-zone/versions.env`의 CNPG · ESO 차트, PostgreSQL · cloudflared · OTel · PgBouncer · 데모 앱 digest), ClusterSecretStore 이름, Deployment · Service · Secret 이름. 남은 일: 공급자 중립 파일(`30-chat-db`, `31-pgbouncer`, `41-cf-edge`)을 `base/`의 같은 파일로 바꾸고 GKE 차이(Gateway 클래스, StorageClass, NetworkPolicy의 DNS 규칙)만 patch로 두기. 다른 overlay(rke2 · eks) 파일은 고치지 않았습니다.

## 아직 없는 것

- Kyverno, OPA, Argo CD: 이 overlay에 없음. 기본 차단은 NetworkPolicy로 직접 둠.
- CNPG 백업(DR-01): 없음. `verify.sh`가 UNKNOWN으로 보고함.
- Executor의 GKE 권한(`pods/exec`, `pods/portforward`): Executor가 GKE에 붙을 신원(GCP 서비스 계정)을 정한 뒤 Role · RoleBinding을 더함.
