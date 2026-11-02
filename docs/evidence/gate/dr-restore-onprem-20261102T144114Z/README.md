# DR-RESTORE 실측: 온프렘 RKE2 `bravo-onprem` (DR-01)

2026-11-02 23:41–23:50 KST. 게이트 DR-RESTORE는 `chat/chat-db-restore`를 읽습니다(`spec.bootstrap.recovery`, `readyInstances >= 1`, phase에 "healthy"). DR-ARCHIVE는 chat-db의 `pg_stat_archiver`를 읽습니다.

## 결과

| 항목 | 값 |
|---|---|
| 백업 저장소 | Rook RGW 버킷 `bravo-chat-db-backup`(첨부 버킷과 다름). 주인은 RGW 사용자 `chat-db-backup` 하나. Block Public Access, TLS 아닌 요청 거부 정책 |
| 키 | 플랫폼 OpenBao `secret/envs/target-onprem/chat-db-backup`(`access_key_id`, `secret_access_key`)에서 만들고 파이프로 Secret `rook-ceph/chat-db-backup-keys`, `chat/chat-db-backup-s3`에 넣음(D-26). RGW가 만든 키가 아님 |
| 백업 | `Backup chat-db-20261102t144846z`, backupId `20261102T144846`, completed 2026-11-02 23:48:49 KST, WAL `…12` |
| 복구 목표 | 복원 지점 `dr-20261102t144846z`, LSN `0/13000130`(23:48:56 KST). 표를 `SHARE` 잠금한 채로 센 뒤 만든 지점 |
| 복구 클러스터 | `chat/chat-db-restore` "Cluster in healthy state", readyInstances 1, timeline 2. 23:49:43 KST(만들고 41초) |
| 복구가 멈춘 곳 | `00000002.history`: `1  0/13000130  at restore point "dr-20261102t144846z"`(09 파일) |
| 행 수 | 7개 표 모두 같음(아래 표) |
| DR-ARCHIVE | 바꾸기 전 archived 11 · failed 0. 바꾼 뒤 archived 21, failed 1(`…0C`, 23:45:05 KST, 재시작 중 옛 Pod), 마지막 성공 `…13` 23:48:57 KST. 실패가 성공보다 앞이라 PASS 조건 그대로 |
| chat-db 중단 | 23:45:05–23:45:13 KST, 약 8초(`pg_isready` 실패 구간). 앱 엔드포인트 2개는 계속 Ready |
| 논리 복제 | 구독 `bravo_fwd_sub`(출발지 → 온프렘) 작업자 다시 돎, `received_lsn` 진행(06, 08 파일) |
| NetworkPolicy | 추가 안 함. chat 네임스페이스에 기본 차단이 없고 정책은 앱 · 링크 Pod만 고름(측정 03 파일 시점). chat-db → RGW 443은 이미 열려 있음 |
| Terraform | 없음(온프렘은 클러스터 안 RGW) |

| 표 | chat-db(복원 지점) | chat-db-restore |
|---|---|---|
| public.attachment_store | 1 | 1 |
| public.attachments | 3 | 3 |
| public.bravo_barrier | 0 | 0 |
| public.bravo_fence | 1 | 1 |
| public.message_attachments | 3 | 3 |
| public.messages | 1021 | 1021 |
| public.write_block | 0 | 0 |

## 고른 방법과 이유(세 환경 같음)

CNPG 1.30.1 내장 `spec.backup.barmanObjectStore` + 이미지 `postgresql:16.15-system-bookworm@sha256:639ef58e…`.

- 지금 이미지(`standard`)에는 barman-cloud가 없음. `system` 이미지는 같은 PostgreSQL 빌드(16.15-1.pgdg12+2, pgaudit 16.1, pgvector 0.8.7)에 barman-cloud 3.20.1만 더한 것. arm64 판으로 노트북에서 실행해 확인.
- Barman Cloud Plugin v0.15.1은 cert-manager가 필요함. 출발지 · 온프렘에는 cert-manager가 없음. 세 클러스터에 구성 요소 둘(cert-manager, 플러그인)과 그 웹훅 통로를 더 깔아야 함.
- 대가: 적용할 때 CNPG가 경고를 냄. "Native support for Barman Cloud backups and recovery is deprecated and will be completely removed in CloudNativePG 1.31.0"(04 파일). 그래서 CNPG를 1.31로 올리기 전에 플러그인으로 옮겨야 함. 버킷 · 경로 · 키는 그대로 씀(같은 barman-cloud 형식).

## 파일

| 파일 | 무엇 |
|---|---|
| `01-rgw-backup-bucket.txt` | `scripts/rgw-backup-bucket.sh`: OpenBao 키 생성, Secret, RGW 사용자, 버킷, 공개 차단, TLS 정책 |
| `02-preflight.txt` | chat-db를 건드리기 전 시험 Pod: system 이미지(aarch64)로 `barman-cloud-check-wal-archive`, boto3 쓰기 · 지우기 |
| `03-state-before.txt` | 바꾸기 전 archiver, 구독, 슬롯, Pod |
| `04-patch-chat-db.txt` | chat-db 병합 패치(imageName, backup)와 CNPG 경고 |
| `05-downtime-monitor.txt` | 0.5초마다 `pg_isready`와 앱 Ready 엔드포인트 수, 바뀐 순간만 |
| `06-state-after-restart.txt` | 재시작 뒤 archiver, 구독(온프렘) · 슬롯(출발지) |
| `07a-dr-restore-first-attempt-backupid-missing.txt` | 첫 시도: CNPG가 `targetName`에 `recoveryTarget.backupID`를 요구해 거부. 템플릿에 backupID를 넣고 다시 함 |
| `07-dr-restore-run.txt` | `scripts/dr-restore.sh onprem` 전체: 백업, 복원 지점과 행 수, 복구 클러스터, 비교 |
| `08-state-after.txt` | 끝난 뒤 archiver, 복제, 복구 클러스터 슬롯(0개), Backup 목록 |
| `09-timeline-history.txt` | 복구 클러스터의 timeline 2 기록(복원 지점에서 멈춤) |

## 다시 하는 법

```
deploy/landing-zone/scripts/rgw-backup-bucket.sh bravo-onprem target-onprem   # install.sh 단계 db_backup
deploy/landing-zone/scripts/dr-restore.sh onprem                              # 백업 → 복원 지점 → 복구 → 비교
```

복구 클러스터는 chat-db의 PostgreSQL 설정을 그대로 받고 `max_logical_replication_workers = 0`을 더함. 복원된 카탈로그에 구독 `bravo_fwd_sub`(활성)가 남아 있어서, 이 복제본이 출발지 슬롯을 가져가지 못하게 막는 설정임. 실측: 적용 작업자 0개.

## 남은 것

- CNPG 1.31 전에 Barman Cloud Plugin으로 옮기기(위 경고).
- 백업 일정(`ScheduledBackup`)은 넣지 않음. 지금은 `dr-restore.sh`가 부를 때만 base backup을 찍고, WAL은 계속 보관됨(보존 7일).
- 복구 클러스터는 Backup 상태에 적힌 같은 Secret `chat/chat-db-backup-s3`(읽기 · 쓰기 키)로 읽음. 클라우드 두 곳처럼 읽기만 하는 자격은 온프렘에 아직 없음.
- 게이트 등록부에 `restore_cluster: chat/chat-db-restore`를 넣는 일은 게이트 담당(이 작업 밖).
