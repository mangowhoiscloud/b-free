# DR-RESTORE 실측: GKE `bravo-target-gcp` (DR-01)

2026-11-02 23:43 – 2026-11-03 00:10 KST. 게이트 DR-RESTORE는 `chat/chat-db-restore`를 읽고, DR-ARCHIVE는 chat-db의 `pg_stat_archiver`를 읽습니다.

## 결과

| 항목 | 값 |
|---|---|
| 백업 저장소 | GCS `bravo-target-gcp-db-backup-3f4e9e`(asia-northeast1, 첨부 버킷과 다름). 공개 접근 방지 enforced, 균일 버킷 접근, CMEK(전용 키 `db-backup`) |
| 자격 | Workload Identity Federation for GKE. KSA 주체(`principal://…/subject/ns/chat/sa/chat-db`)에 버킷 역할을 바로 줌: `chat-db`는 `storage.objectUser` + `storage.legacyBucketReader`, `chat-db-restore`는 `storage.objectViewer` + `storage.legacyBucketReader`. GSA, HMAC, JSON 키 없음. CNPG 설정은 `googleCredentials.gkeEnvironment: true` |
| Terraform | `infra/envs/target-gcp/db_backup.tf`. plan을 읽고 apply: 7개 추가(키, 키 IAM, 버킷, 버킷 IAM 4), 바꿈 0, 지움 0. `-target`으로 이 파일 자원만 |
| NetworkPolicy | chat에 `default-deny`가 있음(측정). 둘 추가(`overlays/gke/manifests/60-networkpolicies.yaml`): `chat-db-backup`(chat-db와 chat-db-restore Pod → 메타데이터 서버 169.254.169.254:80 · 169.254.169.252:988, 사설 대역을 뺀 주소의 443), `chat-db-restore`(들어옴은 cnpg-system에서 8000 · 5432만, 나감은 API 서버 `10.30.0.3/32:443`만). FQDN 정책이 꺼진 클러스터라 GCS는 "공개 주소 443"보다 좁힐 수 없음 |
| 백업 | `Backup chat-db-20261102t150809z`, backupId `20261102T150811`, completed 2026-11-03 00:08:14 KST |
| 복구 목표 | 복원 지점 `dr-20261102t150809z`, LSN `0/F000130`. 표를 `SHARE` 잠금한 채로 센 뒤 만든 지점 |
| 복구 클러스터 | `chat/chat-db-restore` "Cluster in healthy state", readyInstances 1, timeline 2. 00:09:33 KST. 저장소 4Gi |
| 복구가 멈춘 곳 | `00000002.history`: `1  0/F000130  at restore point "dr-20261102t150809z"`(09 파일) |
| 행 수 | 7개 표 모두 같음(아래 표). 이 DB는 아직 전환 전 목적지라 행이 있는 표는 2개뿐 |
| DR-ARCHIVE | 바꾸기 전 archived 7 · failed 0. 끝난 뒤 archived 17, failed 1(`…08`, 23:56:15 KST, 재시작 중 옛 Pod), 마지막 성공 `…0F` 00:08:23 KST. 실패가 성공보다 앞이라 PASS 조건 그대로 |
| chat-db 중단 | 23:56:16–23:56:29 KST, 약 13초. 붙어 있던 연결은 운영자 psql 하나뿐. 앱 엔드포인트 2개는 계속 Ready |
| 논리 복제 | GKE chat-db에는 구독이 없고 슬롯 `bravo_rev_sub`는 전후 모두 inactive(같은 LSN) |

| 표 | chat-db(복원 지점) | chat-db-restore |
|---|---|---|
| public.attachment_store | 1 | 1 |
| public.attachments | 0 | 0 |
| public.bravo_barrier | 0 | 0 |
| public.bravo_fence | 1 | 1 |
| public.message_attachments | 0 | 0 |
| public.messages | 0 | 0 |
| public.write_block | 0 | 0 |

## 고른 방법

세 환경 같음: CNPG 1.30.1 내장 `spec.backup.barmanObjectStore` + `postgresql:16.15-system-bookworm@sha256:639ef58e…`. 이유와 대가(CNPG 1.31에서 내장 방식 제거)는 `../dr-restore-onprem-20261102T144114Z/README.md`. system 이미지의 google-cloud-storage 3.13이 메타데이터 서버로 KSA 토큰을 받음(02 파일: ADC, 버킷 확인, CMEK `db-backup` 쓰기 · 지우기).

## 첫 시도 실패(07a 파일)

복구 클러스터 저장소를 1Gi로 만들었더니 PVC가 묶이지 않았습니다. GCE 오류: "Disk size cannot be smaller than 4 GB for disk type hyperdisk-balanced". `bravo-db`는 C4A 노드라 Hyperdisk Balanced입니다. 템플릿 크기를 변수로 바꾸고 GKE는 4Gi로 다시 했습니다(그때 찍은 백업 `chat-db-20261102t145706z`는 completed로 남음).

## 파일

| 파일 | 무엇 |
|---|---|
| `00-terraform-plan.txt`, `00-terraform-apply.txt` | 대상 지정 plan(7 추가)과 apply 결과 |
| `01-networkpolicies.txt` | 적용한 정책 둘(렌더 결과)과 그전 chat 정책 이름 |
| `02-preflight.txt` | chat-db를 건드리기 전 시험 Pod: aarch64, `barman-cloud-check-wal-archive`, ADC, CMEK 쓰기 · 지우기 |
| `03-state-before.txt` | 바꾸기 전 archiver, 슬롯, Pod |
| `04-patch-chat-db.txt` | chat-db 병합 패치(imageName, backup)와 CNPG 경고 |
| `05-downtime-monitor.txt` | 0.5초마다 `pg_isready`와 앱 엔드포인트 |
| `06-state-after-restart.txt` | 재시작 뒤 archiver |
| `07a-dr-restore-first-attempt-1Gi-hyperdisk.txt` | 위 첫 시도 |
| `07-dr-restore-run.txt` | `scripts/dr-restore.sh gke` 전체 |
| `08-state-after.txt` | 끝난 뒤 archiver, 복구 클러스터, Backup, PVC, chat 정책 |
| `09-timeline-history.txt` | 복구 클러스터의 timeline 2 기록(복원 지점에서 멈춤) |

## 다시 하는 법

```
terraform -chdir=infra/envs/target-gcp apply      # db_backup.tf (plan을 먼저 읽기)
infra/envs/target-gcp/scripts/up.sh               # 60-networkpolicies, 30-chat-db(DB_BACKUP_BUCKET은 Terraform 출력)
deploy/landing-zone/scripts/dr-restore.sh gke
```

## 남은 것

- CNPG 1.31 전에 Barman Cloud Plugin으로 옮기기. `ScheduledBackup`은 아직 없음.
- GCS 송신을 FQDN으로 좁히려면 클러스터에 FQDN 네트워크 정책을 켜야 함(이 작업에서는 안 함).
