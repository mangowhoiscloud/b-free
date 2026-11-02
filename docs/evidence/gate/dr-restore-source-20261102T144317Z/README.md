# DR-RESTORE 실측: 출발지 EKS `bravo-source` (DR-01)

2026-11-02 23:43–23:56 KST. 게이트 DR-RESTORE는 `chat/chat-db-restore`를 읽고, DR-ARCHIVE는 chat-db의 `pg_stat_archiver`를 읽습니다.

## 결과

| 항목 | 값 |
|---|---|
| 백업 저장소 | S3 `bravo-source-db-backup-f93be8419730a1fbb0cff7d65a`(ap-northeast-2, 첨부 버킷과 다름). 공개 접근 차단 넷, BucketOwnerEnforced, SSE-KMS(앱 키, 버킷 키), TLS 아닌 요청 거부 |
| 자격 | EKS Pod Identity(이 클러스터가 이미 쓰는 방식). `chat/chat-db` → 역할 `bravo-source-chat-db-backup-…`(목록 · 읽기 · 쓰기 · 지우기), `chat/chat-db-restore` → `bravo-source-chat-db-restore-…`(목록 · 읽기). 정적 AWS 키 없음. CNPG 설정은 `s3Credentials.inheritFromIAMRole: true` |
| Terraform | `infra/envs/source-aws/db_backup.tf`. plan을 읽고 apply: 11개 추가, 바꿈 0, 지움 0. 다른 갈래 자원을 건드리지 않게 `-target`으로 이 파일 자원만 |
| 백업 | `Backup chat-db-20261102t145421z`, backupId `20261102T145438`, completed 2026-11-02 23:54:45 KST, WAL `…17` |
| 복구 목표 | 복원 지점 `dr-20261102t145421z`, LSN `0/18000130`(23:54:55 KST). 표를 `SHARE` 잠금한 채로 센 뒤 만든 지점 |
| 복구 클러스터 | `chat/chat-db-restore` "Cluster in healthy state", readyInstances 1, timeline 2. 23:55:54 KST(만들고 약 57초). 복구 Pod도 Pod Identity 자격을 받음(08 파일) |
| 복구가 멈춘 곳 | `00000002.history`: `1  0/18000130  at restore point "dr-20261102t145421z"`(09 파일) |
| 행 수 | 7개 표 모두 같음(아래 표) |
| DR-ARCHIVE | 바꾸기 전 archived 19 · failed 0. 끝난 뒤 archived 25 · failed 0, 마지막 `…18` 23:54:56 KST |
| chat-db 중단 | 새 연결 거부 23:50:10–23:53:23 KST, 3분 13초. 그중 약 3분은 CNPG 스마트 종료가 기존 세션이 끝나기를 기다린 시간(`smartShutdownTimeout` 기본 180초, 05b 이벤트: 14:50:09Z Killing → 14:53:15Z 새 Pod). 출발지 chat-db에는 세 환경(aws, gcp, onprem) 앱 Pod 4개의 `app_writer` 세션과 온프렘 구독의 walsender가 붙어 있음(재시작 뒤 23:57 KST 측정). 그동안 기존 세션은 계속 동작, 앱 Pod 2개는 Ready 유지, 끝난 뒤 공개 주소 `/readyz` 200 |
| 논리 복제 | 슬롯 `bravo_fwd_sub` 다시 active. 온프렘 구독 `received_lsn` 0/16000060 → 0/19000060 진행 |
| NetworkPolicy | 추가 안 함. chat 네임스페이스에 기본 차단이 없고, 정책 · CiliumNetworkPolicy는 앱 · 링크 Pod만 고름(측정). chat-db → S3 443, Pod Identity 에이전트 169.254.170.23:80은 이미 열림 |

| 표 | chat-db(복원 지점) | chat-db-restore |
|---|---|---|
| public.attachment_store | 1 | 1 |
| public.attachments | 3 | 3 |
| public.bravo_barrier | 0 | 0 |
| public.bravo_fence | 1 | 1 |
| public.message_attachments | 3 | 3 |
| public.messages | 1021 | 1021 |
| public.write_block | 3 | 3 |

## 고른 방법

세 환경 같음: CNPG 1.30.1 내장 `spec.backup.barmanObjectStore` + `postgresql:16.15-system-bookworm@sha256:639ef58e…`. 이유와 대가(CNPG 1.31에서 내장 방식 제거, 그 전에 Barman Cloud Plugin으로 옮김)는 `../dr-restore-onprem-20261102T144114Z/README.md`. system 이미지의 boto3 1.43은 Pod Identity 자격(`AWS_CONTAINER_CREDENTIALS_FULL_URI`)을 읽음(02 파일에서 호출자가 백업 역할로 나옴).

## 파일

| 파일 | 무엇 |
|---|---|
| `00-terraform-plan.txt`, `00-terraform-apply.txt` | 대상 지정 plan(11 추가)과 apply 결과 |
| `02-preflight.txt` | chat-db를 건드리기 전 시험 Pod(SA `chat-db`): aarch64, `barman-cloud-check-wal-archive`, STS 호출자, KMS 암호화 쓰기 · 지우기 |
| `03-state-before.txt` | 바꾸기 전 archiver, 구독 · 슬롯(출발지와 온프렘), Pod |
| `04-patch-chat-db.txt` | chat-db 병합 패치(imageName, backup)와 CNPG 경고 |
| `05-downtime-monitor.txt`, `05b-chat-db-events.txt` | 0.5초마다 `pg_isready`와 앱 엔드포인트, chat-db 이벤트 |
| `06-state-after-restart.txt` | 재시작 뒤 archiver와 복제 |
| `07-dr-restore-run.txt` | `scripts/dr-restore.sh source` 전체 |
| `08-state-after.txt` | 끝난 뒤 archiver, 복제, 복구 클러스터, 복구 Pod의 SA와 자격 변수 이름 |
| `09-timeline-history.txt` | 복구 클러스터의 timeline 2 기록(복원 지점에서 멈춤) |

## 다시 하는 법

```
terraform -chdir=infra/envs/source-aws apply      # db_backup.tf (plan을 먼저 읽기)
deploy/landing-zone/overlays/eks/install.sh bravo-source cnpg   # chat-db.yaml에 backup, DB_BACKUP_BUCKET은 Terraform 출력
deploy/landing-zone/scripts/dr-restore.sh source
```

## 남은 것

- 다음 chat-db 재시작에서도 스마트 종료가 최대 3분 새 연결을 막음. 줄이려면 `smartShutdownTimeout`을 낮춰야 함(이 작업에서는 바꾸지 않음).
- CNPG 1.31 전에 Barman Cloud Plugin으로 옮기기. `ScheduledBackup`은 아직 없음.
