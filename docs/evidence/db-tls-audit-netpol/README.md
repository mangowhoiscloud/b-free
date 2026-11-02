# DB-TLS · DB-AUDIT · NET-DEFAULT-DENY 보완 (2026-11-03 01:23–02:05 KST)

게이트 실행 `platform-gate-20261102T161443Z`의 FAIL 세 가지를 환경에서 고쳤습니다. 고친 뒤 게이트 실행은 `docs/evidence/gate/platform-gate-20261102T170251Z/`입니다. 두 쌍 모두 DB-TLS · DB-AUDIT · NET-DEFAULT-DENY · NET-PATHS · DR-ARCHIVE · DR-RESTORE가 양쪽 PASS입니다. 남은 FAIL은 이 작업 밖입니다(GKE OBJ-06, 온프렘 RGW OBJ-01 · OBJ-05).

## 무엇을 바꿨나

| 항목 | 바꾼 것 | 파일 |
|---|---|---|
| DB-TLS (서버) | 세 chat-db `pg_hba`에 `hostnossl all all all reject`, `hostssl all all all scram-sha-256`. CNPG 기본 `host all all all` 앞에 들어감 | `deploy/landing-zone/overlays/eks/chat-db.yaml`, `base/chat-db.yaml`(온프렘), `overlays/gke/manifests/30-chat-db.yaml` |
| 링크 이름 SAN | `serverAltDNSNames`: 출발지 `src-db-link.chat.svc[.cluster.local]`, `src-db` · `src-pgb.bravo-link.svc[.cluster.local]`, 온프렘 `dst-*` 같은 짝, GKE `gke-db.bravo-link.svc[.cluster.local]`, 세 곳 모두 `chat-pgbouncer.chat.svc[.cluster.local]` | 같은 세 파일 |
| CA 묶음 | ConfigMap `chat/bravo-db-ca`(세 CNPG CA, 공개 인증서만)를 세 클러스터에 | `deploy/landing-zone/scripts/db-ca.sh`, 두 install.sh의 `db_ca` 단계 |
| 구독(양방향) | CNPG `projectedVolumeTemplate`로 `/projected/db-ca/ca.crt`. 온프렘 `bravo_fwd_sub`, 출발지 `bravo_rev_sub` 연결 문자열에 `sslmode=verify-full sslrootcert=/projected/db-ca/ca.crt`(비밀번호는 DB 안에서 그대로 두고 덧붙임) | 세 chat-db 파일, `deploy/cloud/in-cluster/topology.json`의 `replication` |
| PgBouncer 서버 쪽 | `server_tls_sslmode = verify-full`, `server_tls_ca_file = /etc/pgbouncer/db-ca/ca.crt` | `base/chat-pgbouncer.yaml`, `overlays/gke/manifests/31-pgbouncer.yaml` |
| PgBouncer 클라이언트 쪽 | `client_tls_sslmode = require`. 인증서는 같은 클러스터 chat-db 서버 인증서(Secret `chat-db-server`, CNPG가 만들고 갱신) | 같은 두 파일 |
| 앱 | DSN에 `sslmode=verify-full sslrootcert=/var/run/bravo/db-ca/ca.crt`, CA 마운트 | 두 install.sh(`bravo-demo-db`), `overlays/gke/manifests/21-externalsecrets.yaml`, `overlays/eks/chat-values.yaml`(새 파일), `overlays/rke2/chat-values.yaml`, `overlays/gke/values/chat.yaml` |
| Executor | DB · PgBouncer 연결 4개에 `sslmode=verify-full`, 환경별 CNPG CA. CA는 ConfigMap `bravo-control/executor-kube-ca`(`/etc/bravo/kube/*-chat-db.crt`) | `deploy/cloud/in-cluster/topology.json`, `deploy/platform/scripts/cluster-cas.sh` |
| DB-AUDIT | 세 곳 `pgaudit.log = ddl, role`, `log_connections` · `log_disconnections = on`. 온프렘 · GKE는 pgaudit가 새로 실림(CNPG가 `shared_preload_libraries`와 확장을 더함) | 세 chat-db 파일 |
| 기본 차단 | 출발지 · 온프렘 `chat`에 `default-deny`(전체 Pod, 수신 · 송신) + 허용 규칙. 앱 규칙은 차트 값 | `base/chat-netpol.yaml`, `overlays/eks/chat-netpol.yaml`, `overlays/rke2/chat-netpol.yaml`, `overlays/eks/chat-values.yaml` |
| Secret 전달 | Secret을 싣는 `kubectl apply -f -`를 `apply --server-side --force-conflicts --field-manager=bravo-secrets -f -`로(3b4a910과 같음) | 두 install.sh |

## 홉별 TLS 검증 상태(측정)

| 홉 | 모드 | 증거 |
|---|---|---|
| 앱 → PgBouncer | verify-full(이름 `chat-pgbouncer.chat.svc.cluster.local`) | `pgbouncer-tls-show.txt`(세 환경 SHOW CLIENTS 모두 TLSv1.3) |
| PgBouncer → chat-db(지금 대상) | verify-full | `pgbouncer-tls-show.txt`(SHOW SERVERS TLSv1.3) |
| PgBouncer → 컷오버 뒤 대상 | verify-full과 같은 검사로 확인. 틀린 이름은 `hostname mismatch` | `pgbouncer-other-target-tls.txt` |
| 정방향 구독(온프렘 → 출발지) | verify-full, 실제 streaming | `subscriptions-verify-full.txt`, `replication-both-directions.txt` |
| 역방향 구독(출발지 → 온프렘) | verify-full. 구독은 컷오버 때 Executor가 켜므로 꺼 둔 채, 같은 설정으로 복제 접속(`IDENTIFY_SYSTEM`) 성공 | `replication-both-directions.txt` |
| Executor → DB · PgBouncer(D-16 링크) | verify-full. 대조: 다른 CA는 `certificate verify failed`, IP는 이름 불일치, 평문은 `pg_hba` 거부 | `executor-verify-full.txt`, `executor-contract-readonly.txt` |
| 게이트 `gate_ro` | 이미 verify-full(`verify_full_ok`) | 게이트 표 |
| 수집기 | 고객 DB에 붙지 않음(Deployment에 DSN 환경 변수 없음, 02:03 확인) | - |

평문은 세 DB 모두 `pg_hba.conf rejects connection ... no encryption`입니다(`onprem-phase1-tls-from-source.txt`, `source-gke-phase1-tls.txt`). verify-ca로 낮춘 홉은 없습니다.

## 컷오버 경로가 깨지지 않았나(측정)

- 앱 끝점: src · dst · gke · 대표 이름 모두 `/healthz` · `/readyz` 200(`after-health.txt`).
- 복제: 정방향 streaming, lag 0(Executor `/v1/readiness`). 역방향은 위 표.
- 계약 시험: E4(observe · stats 자격으로 쓰기 · PAUSE · RELOAD 거부)를 executor-0 안에서 새 topology(verify-full)로 실행, 두 쪽 `all_refused: true`. 풀 · 진입 · 펜스 읽기도 통과(`executor-contract-readonly.txt`). E5 · E1 · E6은 콘솔에서 사람이 만든 cutover 승인 두 개가 있어야 해서 실행하지 않음.
- 백업: 기본 차단 아래 WAL 보관(출발지 S3 + Pod Identity, 온프렘 RGW)과 수동 Backup 두 개 completed(`backup-and-wal-archive.txt`). 게이트 DR-ARCHIVE · DR-RESTORE PASS.
- Hubble: 정책 전 20분 흐름 기록(`hubble-baseline-before.txt`), 적용 뒤 DROPPED 전부와 해석(`hubble-drops-after.txt`). 적용 순간과 PgBouncer 재시작 순간의 일시 차단, 게이트 NET-PATHS의 거부 경로 시험만 있음.

## 측정한 비용과 주의

- 출발지 chat-db 재시작(투영 볼륨 추가) 01:36:32–01:39:56. CNPG smart shutdown이 180초 동안 기존 연결을 기다림. 그동안 기존 PgBouncer 연결로 앱 `/readyz`는 3.5초 간격 표본 모두 200(`source-restart-readyz.txt`). 온프렘 · GKE 재시작(pgaudit 적재)은 약 20초.
- 온프렘 PgBouncer TLS 전환 직후 16:40:50 서버 접속 몇 번이 `unexpected eof`로 실패한 뒤 회복. 이후 오류 없음.
- 정방향 구독이 `wal_sender_timeout = 5s`(CNPG 기본) 때문에 링크 위에서 가끔 끊기고 몇 초 안에 다시 붙음(16:41, 16:47, 17:01 출발지 로그). 이번 변경 전부터 있던 동작입니다. 02:08 KST에 60초로 바꾼 뒤 22분 동안 끊김 0번(`docs/evidence/repl-timeouts/`).
- `log_connections`로 CNPG `pg_isready`(10초마다) 접속도 로그에 남습니다.
- PgBouncer는 CNPG 서버 키를 마운트합니다(같은 네임스페이스, 같은 신뢰 경계). CNPG가 인증서를 갱신하면(만료 7일 전) PgBouncer에 RELOAD가 필요합니다. CA 만료는 2027-01-31입니다. CA가 바뀌면 `db-ca.sh`와 `cluster-cas.sh`를 다시 돌립니다.
- 데모 이야기: `verify.sh`는 전에 출발지 `chat`의 기본 차단 없음을 "S2 변경 승인으로 고칠 틈"으로 표시했습니다. 이제 기본 차단이 있으므로 S2 데모에 이 틈을 쓸 수 없습니다.

## push 뒤에 바뀌는 것

`deploy/cloud/in-cluster/topology.json`은 Argo CD ConfigMap `executor-topology`입니다. push와 동기화 뒤 executor-0이 새 topology(verify-full)로 다시 뜹니다. CA 파일(`executor-kube-ca`)은 이미 들어가 있고, 같은 연결 문자열을 executor-0 안에서 미리 시험했습니다(`executor-verify-full.txt`).
