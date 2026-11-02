# envs/target-gcp

GCP 목적지입니다(D-01, D-21, D-22, D-23). GKE Standard + Dataplane V2에 온프렘 K3s · EKS와 같은 랜딩 존(`deploy/landing-zone/overlays/gke`)과 데모 앱을 올립니다. 전용 프로젝트 `b-free-target-20261102`(팀장 승인 2026-11-02)에서 실제로 올렸습니다.

## 명령 하나

```
infra/envs/target-gcp/scripts/up.sh       # Terraform apply → 랜딩 존 → 앱(멱등, 다시 돌려도 됨)
infra/envs/target-gcp/scripts/verify.sh   # PASS/FAIL/UNKNOWN과 증거(runs/verify-<UTC>/)
infra/envs/target-gcp/scripts/down.sh     # Gateway · DB 디스크 정리 → HMAC 키 · AppRole 회수 → terraform destroy
```

준비물: Terraform 1.17, gcloud(프로젝트 권한), kubectl, helm, jq, envsubst, openssl, skopeo, AWS 자격(상태 백엔드 · 출발지 ECR 읽기), 열려 있는 플랫폼 OpenBao(`deploy/pilot/scripts/keys.sh status`, 위치는 custody `bao_target`). kubectl 컨텍스트는 `bravo-target-gcp`이고 kubeconfig는 `infra/envs/target-gcp/.terraform/bravo/kubeconfig`(비밀 없음, DNS 엔드포인트 + `gke-gcloud-auth-plugin`)입니다. gcloud 기본 설정은 쓰지도 바꾸지도 않습니다. 모든 호출에 `--project`를 줍니다.

## 무엇을 만드나(Terraform)

| 자원 | 설정 |
|---|---|
| VPC | 노드 서브넷 `10.30.0.0/24`(Pod `10.72.0.0/16`, Service `10.73.0.0/16`), 프록시 전용 서브넷 `10.30.4.0/23`(Gateway), Private Google Access |
| Cloud NAT | 노드는 공인 IP 없음. 바깥으로는 NAT로만. 바깥 80 차단(내부 80은 허용) |
| GKE | Standard, zonal(`asia-northeast1-b`), `ADVANCED_DATAPATH`(Dataplane V2), 1.36(REGULAR), 사설 노드 · 사설 엔드포인트 + IAM DNS 엔드포인트, Workload Identity(`GKE_METADATA`), Gateway API, Secret CMEK, 관리형 Hubble relay |
| 노드 풀 `core` | `c4a-standard-4`(Arm) 1–2대, `hyperdisk-balanced` 50 GiB + CMEK, Arm taint 끔 |
| 이미지 저장소 | Artifact Registry `bravo`(불변 태그). 출발지와 같은 digest만 둠. `scripts/images.sh`(up.sh 2단계)가 출발지 ECR `bravo-source/<이름>`에서 `skopeo copy --preserve-digests --multi-arch all`로 digest째 복사합니다. GKE용으로 따로 빌드하지 않습니다(2026-11-03부터, 그전 GKE 앱은 따로 빌드한 `df08e969…`). 자격은 AWS · gcloud 체인에서 받아 파이프(`/dev/fd`)로만 넘김 |
| 첨부 버킷 | 공개 접근 방지 `enforced`, 균일 접근, 버전 관리, CMEK, 시험 객체 `_gate/canary` |
| DB 백업 버킷(`db_backup.tf`) | CNPG chat-db의 WAL과 base backup(barman-cloud, DR-01). 공개 접근 방지 `enforced`, 균일 접근, CMEK 키 `db-backup`. 키 없이 Workload Identity 주체에 바로 권한: KSA `chat/chat-db` 읽기 · 쓰기, `chat/chat-db-restore` 읽기 |
| 서비스 계정 | `chat-app`(버킷 `objectUser`, HMAC 주인), `gate-other`(권한 없음, OBJ-05), 노드 SA(최소 권한 + 저장소 읽기) |
| Gateway 주소 | 고정 내부 주소 `bravo-target-gcp-gateway` |
| 라벨 | 모든 자원 `component=target`, `env=target-gcp`, `managed-by=terraform`, `owner=team-bravo` |

비밀 값은 Terraform이 만들지 않습니다(D-26). HMAC 키, DB 비밀번호, 관리 토큰은 `scripts/secrets.sh`가 플랫폼 OpenBao `secret/envs/target-gcp/*`에 파이프로만 씁니다. 터널 토큰은 `keys.sh issue-tunnel-token target-gcp <tunnel_id>`입니다.

## 상태 저장: 다른 환경과 같은 S3 백엔드

`backend.hcl` → `s3://bravo-tfstate-721622471953-apne2/envs/target-gcp.tfstate`(잠금 파일, 암호화).

- 상태는 플랫폼이 쥡니다(TECH-STACK-SPEC 3절 #1). 고객 프로젝트 안 GCS에 두면 프로젝트를 지울 때 상태가 함께 사라지고, 플랫폼이 고객 클라우드마다 다른 상태 저장소를 운영하게 됩니다.
- 경계는 S3 API입니다. `endpoints.s3`만 바꾸면 RGW 같은 S3 호환 저장소로 옮길 수 있습니다. 출구전략 제품("Beyond Lock-In")이 자기 상태도 한 클라우드의 고유 API에 묶지 않는다는 원칙입니다(TECH-STACK-SPEC 0절 "출구가 있다").
- 남는 의존: 지금 버킷은 플랫폼 AWS 계정에 있습니다. 플랫폼을 옮기면 상태도 S3 API로 옮깁니다.

## 스펙과 다른 것(숨기지 않음)

| 항목 | 내용 |
|---|---|
| 리전 | 도쿄 `asia-northeast1`. 서울에는 Arm(C4A)이 없음. 데모 이미지가 arm64 하나라 같은 digest를 띄우려면 Arm이 필요 |
| 블록 저장 | `pd-balanced`가 아니라 `hyperdisk-balanced`(C4A 제약) |
| 진입 | Cilium Gateway가 아니라 GKE Gateway `gke-l7-rilb`(Dataplane V2에서 Cilium Gateway를 켤 수 없음) |
| OBJ-06 | GCS XML API는 평문 HTTP를 받음. 막는 조직 정책 `storage.secureHttpTransport`는 조직 아래 프로젝트에서만 걸 수 있어(이 프로젝트는 조직 없음, 403) 걸지 못함. 보완: 노드 바깥 80 차단 방화벽, 앱 NetworkPolicy 443만. 보완 통제는 게이트 질문 OBJ-PLAIN-EGRESS로 잽니다(앱 라벨 Pod에서 `storage.googleapis.com:80` 연결 실패, 앱 엔드포인트 https). `docs/evidence/gate/obj-plain-egress-20261103/` |
| 클러스터 위치 | zonal 제어면(비용 · 생성 시간). EKS는 3-AZ 제어면 |

## 하루 비용(도쿄 정가, 2026-11-02 Cloud Billing Catalog API 조회)

| 항목 | 단가 | 하루(24시간) |
|---|---|---|
| GKE 클러스터 관리(zonal) | $0.10/시간 | $2.40. 결제 계정당 zonal 클러스터 1개는 월 $74.40 무료 크레딧으로 상쇄될 수 있음(같은 결제 계정의 다른 zonal · Autopilot 클러스터가 쓰지 않을 때) |
| 노드 `c4a-standard-4` 1대 | vCPU $0.03963/시간 × 4 + 메모리 $0.004508/GiB·시간 × 16 = $0.2306/시간 | $5.54(2대로 늘면 $11.07) |
| Hyperdisk Balanced: 부팅 50 GiB(3,300 IOPS, 215 MiBps) + DB 10 GiB | 용량 $0.102/GiB·월, 기본 3,000 IOPS · 140 MiBps 초과분 IOPS $0.006/월, MiBps $0.051/월 | 약 $0.40 |
| Cloud NAT | 게이트웨이 $0.0014/VM·시간, NAT IP $0.005/시간, 처리 $0.045/GiB | 약 $0.20(이미지 첫 내려받기 포함) |
| Gateway(내부 지역 Application LB) | 전달 규칙 $0.038/시간, 프록시 인스턴스 $0.025/시간 × 1–3, 처리 $0.008/GiB | $1.51–2.71 |
| Cloud KMS 키 3개, GCS, Artifact Registry, 시스템 로그 | 키 $0.06/월, 나머지는 무료 한도 안 | 약 $0.01 |
| Cloudflare Tunnel | 무료 | $0 |
| **합계** | | **약 $10.1–11.3**(GKE 무료 크레딧 적용 시 약 $7.7–8.9) |

- 프록시 인스턴스 최소 수(1 또는 3)는 공식 가격 페이지에서 확인하지 못했습니다. 그래서 범위로 적습니다.
- 같은 프로젝트의 다른 갈래 자원(예: Cloud SQL `bravo-spike-db-pg16`)은 이 표에 없습니다.
- D-01 비용 상한(GKE 2일분) 기준 약 $20–23입니다. 안 쓸 때는 `scripts/down.sh`로 지웁니다.

## 검증만 할 때

```
terraform -chdir=infra/envs/target-gcp init -backend=false
terraform -chdir=infra/envs/target-gcp validate
```
