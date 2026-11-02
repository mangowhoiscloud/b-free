# DB 이전 매트릭스: 관리형 PostgreSQL을 포함한 아홉 방향

이 문서는 DB 엔진 세 묶음 사이의 아홉 방향 이전을 정합니다. 엔진은 AWS 관리형(RDS for PostgreSQL, Aurora PostgreSQL), Google 관리형(Cloud SQL for PostgreSQL, AlloyDB), 자체 운영(CNPG. 온프렘 K3s, EKS, GKE 위)입니다. 상태는 모두 **제안(D-24)** 입니다. 작성 2026-11-02.

스펙과 기존 문서가 정한 것은 다시 적지 않습니다. 컷오버 8단계와 실패 처리(§8.6, `03-migration.md` 4절), 효과 원장 · `bravo_fence` · 영수증 규칙(`platform/executor/README.md`), 링크와 SAN(`02-network.md` 11절)은 그대로입니다. 이 문서는 한쪽이나 양쪽이 관리형일 때 생기는 빈칸만 채웁니다.

**표기.** 엔진에 관한 사실마다 붙입니다.
- **관찰 [ID]**: 공식 문서에서 직접 확인했습니다. URL은 13절에 있고, 모두 2026-11-02에 접속했습니다.
- **추론**: 공식 문서 여러 개를 조합했거나 설계 판단입니다.
- **확인 필요(SPK-n)**: 공식 근거를 찾지 못했습니다. 11절의 스파이크 시험으로 확인합니다.
- **측정(SPK-n)**: 2026-11-02 실제 클라우드(RDS 서울, Cloud SQL 도쿄)에서 Executor 코드로 돌려 확인했습니다. 결과와 증거 경로는 11.1–11.3절, 칸별 상태는 3.2절입니다. 측정이 위 관찰 · 추론과 다르면 측정을 따릅니다.

약어: **A** = AWS 관리형, **G** = Google 관리형, **C** = CNPG.

## 1. 바뀌지 않는 것과 바뀌는 것

| 바뀌지 않는 것 | 이유 |
|---|---|
| 컷오버 8단계, ABORT · FAILBACK · SAFE_HOLD 규칙 | Executor가 DB에 쓰는 것은 SQL과 PgBouncer 관리 명령뿐입니다. 엔진이 관리형이어도 같은 SQL입니다(추론) |
| `bravo_fence`, barrier 행, 최종 비교, 영수증 형식 | 보통 테이블과 SELECT입니다. 관리형에서도 같습니다(추론) |
| Executor에 클라우드 자격 없음 | 파라미터 그룹, 재부팅, 플래그는 클라우드 API입니다. 그래서 컷오버 밖의 온보딩(5절)에서 IaC나 고객이 합니다 |
| PAUSE는 우리 PgBouncer만 | RDS Proxy에는 일시 정지 기능이 없습니다(관찰 [A8]). Cloud SQL 관리형 풀링 문서에도 PAUSE가 없습니다(관찰 [G7]) |

| 바뀌는 것 | 어디 |
|---|---|
| 관리형 끝에서 논리 디코딩을 켜는 온보딩(재시작이 필요) | 5.1절 |
| 앱과 관리형 DB 사이에 우리 PgBouncer를 끼워 넣는 온보딩 | 5.2절 |
| 관리형 서버 인증서에는 우리 링크 이름이 없음. 접속 이름과 TCP 경로를 나눔 | 6절 |
| 관리형 DB가 구독자일 때 우리 링크에 닿는 길(내부 LB)과 CA | 4.2절, 6절 |
| 펜싱 권한을 관리형 역할 모델에 맞춤 | 4.5절 |

## 2. 엔진별 능력표

| 능력 | RDS for PostgreSQL | Aurora PostgreSQL | Cloud SQL for PostgreSQL | AlloyDB | CNPG |
|---|---|---|---|---|---|
| 논리 디코딩 켜기 | 사용자 파라미터 그룹의 `rds.logical_replication=1`. `wal_level` 등도 같이 바뀜(관찰 [A1]) | DB 클러스터 파라미터 그룹의 같은 값(관찰 [A2]) | 플래그 `cloudsql.logical_decoding=on`(관찰 [G2]) | 플래그 `alloydb.logical_decoding`(관찰 [G9]) | 파일럿에서 동작 |
| 켤 때 재시작 | 필요. static 파라미터(관찰 [A1]) | writer 재부팅(관찰 [A2]) | 필요. primary와 replica가 차례로 재시작(관찰 [G1][G2]) | 필요(관찰 [G9]) | 해당 없음 |
| 발행 · 슬롯 역할 | `rds_replication`. `rds_superuser`가 다른 사용자에게 줄 수 있음(관찰 [A3]) | 같음(관찰 [A2]) | `ALTER USER … WITH REPLICATION`. `cloudsqlsuperuser`가 발행과 구독을 만듦(관찰 [G3]) | `alloydbsuperuser`가 발행 · 구독 생성. REPLICATION 속성 역할 `alloydbreplica`는 내부용이라 줄 수 없음(관찰 [G9]). 복제 연결 사용자는 확인 필요(SPK-11) | REPLICATION 속성(관찰 [P4]) |
| 구독자(CREATE SUBSCRIPTION) | `rds_superuser`. 외부 publisher 구독 절차 있음(관찰 [A4][A5]) | 같음(관찰 [A5]) | `cloudsqlsuperuser`(관찰 [G2]). **HA 인스턴스는 구독자 불가**(관찰 [G2]) | 외부 publisher를 native로 구독하는 공식 절차 없음. 확인 필요(SPK-11) | 선언형 Subscription CRD(관찰 [N1]) |
| PG16 `pg_create_subscription`, `origin = none` | 공식 문서에 언급 없음. 확인 필요(SPK-02) | 같음 | 같음 | 같음 | 코어 기능(관찰 [P1][P2]) |
| 펜싱: `ALTER ROLE … NOLOGIN` | 마스터 사용자는 CREATEROLE(관찰 [A3]). PG16부터 대상 역할의 ADMIN OPTION이 필요(관찰 [P6]) | 같음 | "ALTER ROLE로 모든 사용자의 속성 변경 가능"(관찰 [G3]). ADMIN OPTION 규칙은 같다고 봄(추론) | 확인 필요 | superuser |
| 펜싱: 세션 종료 | `rds_superuser`는 모든 연결을 끊을 수 있음(관찰 [A3]) | 같음 | `pg_signal_backend`를 GRANT하는 절차(관찰 [G4]) | 확인 필요 | superuser |
| 슬롯 WAL 상한 `max_slot_wal_keep_size` | 확인 필요(SPK-01) | 확인 필요 | 플래그 있음, 재시작 불필요(관찰 [G1]) | 확인 필요 | 지금 설정(`03` 4.1) |
| 장애 조치 때 논리 슬롯 | PG17+는 read replica로 슬롯 동기화(관찰 [A6]). Multi-AZ 인스턴스 장애 조치는 확인 필요 | writer 장애 조치 때 유지되는지 확인 필요(SPK-08) | 확인 필요 | 확인 필요 | PG17 failover 슬롯(관찰 [P11]). 우리는 PG16 |
| 서버 인증서 이름 | 인스턴스 엔드포인트가 CN(관찰 [A9]). 사용자 DNS 이름을 넣는 옵션은 문서에서 찾지 못함. RDS Proxy 문서는 custom DNS와 이름 검증을 같이 쓸 수 없다고 적음(관찰 [A8]) | 클러스터 엔드포인트는 SAN 지원 클라이언트 필요(Aurora MySQL 문서, 관찰 [A9]) | CA 모드에 따름. 기본(인스턴스별 CA)은 PSC만 DNS SAN. 공유 CA · 고객 CA는 DNS SAN 있음. 사용자 SAN은 고객 CA만, 최대 3개(관찰 [G5]) | **직접 연결은 verify-ca · verify-full 미지원**. Auth Proxy 권장(관찰 [G10]) | `serverAltDNSNames`(`02` 11.2) |
| TLS 강제 | PG15+ `rds.force_ssl` 기본 1(관찰 [A9]) | 확인 필요 | 확인 필요(`ssl_mode`) | 기본 암호화(관찰 [G10]) | `hostnossl … reject`(`02` 5절) |
| 큰 DB 초기 복사 | 스냅샷 + 따라잡기(4.3절). RDS 단독 절차는 확인 필요(SPK-10) | fast clone + `aurora_volume_logical_start_lsn()` + origin advance 절차 있음(관찰 [A7]) | 확인 필요 | 확인 필요 | 스냅샷 + 따라잡기, PG17+ `pg_createsubscriber`(관찰 [P7]) |
| 최신 메이저 | 18.6까지, 19는 preview(관찰 [A12]) | 18.6.1까지(관찰 [A12]) | 9.6–18, 기본 18(관찰 [G8]) | 14–18(관찰 [G11]) | 1.30이 PG 14–18(관찰 [N1]) |
| 이 설계의 판단 | 지원(P1 후보) | 지원. 슬롯 장애 조치 확인 뒤 | 지원. 단 구독 쪽은 비HA(P2) | **보류**. 3절 막는 것 | 기본 |

**측정으로 채운 칸(2026-11-02, 11.1 · 11.2절).** 위 표의 "확인 필요"보다 아래가 우선입니다.
- `pg_create_subscription`, `origin = none`: RDS · Cloud SQL 모두 비superuser `executor_ctl`이 구독을 만들고 켜고 끔. `pg_replication_origin_advance`는 두 엔진 모두 `executor_ctl`에 거부되고 마스터만 됨.
- 발행 · 슬롯 역할: RDS 마스터는 REPLICATION 속성을 줄 수 없음(`rds_replication`만). Cloud SQL 마스터는 REPLICATION 속성을 줄 수 있음.
- 펜싱(NOLOGIN, REVOKE, 세션 종료): 두 엔진 모두 `executor_ctl`(ADMIN OPTION, `pg_signal_backend`)로 됨.
- `max_slot_wal_keep_size`: RDS는 dynamic, 2048 MB 걸림. Cloud SQL은 102400 MB 미만 거부.
- TLS 강제: RDS(`rds.force_ssl=1`), Cloud SQL(`ENCRYPTED_ONLY`) 모두 평문을 `no pg_hba.conf entry … no encryption`류로 거부.
- 서버 인증서 이름: Cloud SQL 공유 CA 모드 + PSA는 SAN `…sql-psa.goog.`(끝에 점). AlloyDB 직접 연결은 자체 서명 `localhost`.
- Cloud SQL HA 구독자: 생성 · 동작 · 장애 조치 뒤 적용 모두 됨. 문서의 제한 이유는 나가는 IP.

## 3. 아홉 방향 매트릭스

"준비"는 컷오버 전에 끝내는 온보딩입니다(5절). "역방향"은 컷오버 4.1절 4번의 미리 만든 역방향 구독입니다.

| # | 출발 → 목적 | 정방향 방식 | 준비 | 출발지 펜싱 | 역방향(되돌리기) | 막는 것 | 판단 |
|---|---|---|---|---|---|---|---|
| 1 | C → C | 지금 엔진 그대로 | 없음(랜딩 존) | 지금과 같음 | CNPG 발행 → CNPG 구독, `origin = none` | 없음. 파일럿(노트북) 기록 76–139 ms(PostgreSQL 공식 이미지, CNPG 아님, `docs/evidence/pilot-notebook/RUNS.tsv`) | P0 |
| 2 | C → A | CNPG 발행 → RDS · Aurora 구독(`rds_superuser`) | 목적지 IaC: 논리 디코딩을 켠 파라미터 그룹으로 생성(재부팅 없음, 추론). 구독자가 닿을 내부 LB 링크 | 지금과 같음 | RDS · Aurora 발행 → CNPG 구독 | 관리형 구독자가 우리 CA를 읽지 못함(SPK-03). `origin = none` 지원(SPK-02) | 제안. SPK-02 · 03 뒤 |
| 3 | C → G | CNPG 발행 → Cloud SQL 구독(`cloudsqlsuperuser`) | Cloud SQL을 비HA, 공유 CA 모드, 플래그를 켜고 생성 | 지금과 같음 | Cloud SQL 발행 → CNPG 구독 | HA 구독 불가(관찰 [G2]). 구독자 CA(SPK-03). AlloyDB는 보류 | 제안, P2 |
| 4 | A → C | RDS · Aurora 발행 → CNPG 구독 | 출발지 논리 디코딩 켜기(재부팅, 5.1) + PgBouncer 끼워 넣기(5.2) | 4.5절 권한으로 NOLOGIN, REVOKE, 세션 종료 | CNPG 발행 → RDS · Aurora 구독(꺼 둔 채 미리 생성) | 재부팅 일정. 역방향은 #2와 같은 구독자 CA 문제. Aurora 슬롯 장애 조치(SPK-08) | 첫 이전의 중심. P1 후보 |
| 5 | A → A | RDS · Aurora 발행 → RDS · Aurora 구독(링크 경유) | 양쪽 논리 디코딩, 양쪽 내부 LB 링크 | #4와 같음 | 반대 방향 같은 방식 | 양쪽 다 관리형 구독자(SPK-03). Aurora clone 경로는 같은 리전 Aurora 사이만(추론) | P2 |
| 6 | A → G | RDS · Aurora 발행 → Cloud SQL 구독(비HA) | #4의 출발지 준비 + #3의 목적지 준비 | #4와 같음 | Cloud SQL 발행 → RDS 구독 | 양쪽 다 관리형 구독자. Cloud SQL HA | P2. 정방향만이면 Google DMS도 가능(9절) |
| 7 | G → C | Cloud SQL 발행(REPLICATION 속성) → CNPG 구독 | 출발지 플래그 켜기(재시작) + PgBouncer 끼워 넣기 + CA 모드 확인(6절) | `cloudsqlsuperuser`의 ALTER ROLE, `pg_signal_backend` GRANT(관찰 [G3][G4]) | CNPG 발행 → Cloud SQL 구독 | **출발지가 HA면 역방향 구독 불가**(관찰 [G2]). HA를 끄거나 FR-11을 포기해야 함 | 제안, P2 |
| 8 | G → A | Cloud SQL 발행 → RDS 구독 | #7 출발지 준비 + #2 목적지 준비 | #7과 같음 | RDS 발행 → Cloud SQL 구독 | #7의 HA 문제 + 관리형 구독자 CA | P2 |
| 9 | G → G | Cloud SQL 발행 → Cloud SQL 구독. 공식 예시가 이 경우(관찰 [G2]) | 양쪽 플래그, 구독 쪽 비HA | #7과 같음 | 반대 방향 같은 방식 | 양쪽 비HA가 필요 | P2 |

**AlloyDB.** 세 칸 모두 보류합니다. 직접 연결에서 verify-full을 쓸 수 없고(관찰 [G10], **측정(SPK-11)**: 직접 연결 인증서는 `CN=unused`, SAN `localhost`인 자체 서명이고 API가 주는 CA `root.alloydb`로 검증되지 않음), 복제 연결에 쓸 REPLICATION 역할을 사용자에게 주지 못하며(관찰 [G9], 시험 안 함), 외부 publisher를 native로 구독하는 공식 절차가 없습니다. SPK-11이 통과하기 전에는 Google 관리형 선택지를 Cloud SQL로 둡니다.

### 3.1 같은 엔진, 다른 리전 · 계정

| 경우 | 방식 | 이유 |
|---|---|---|
| C → C (서울 → 도쿄, 다른 고객 계정) | 매트릭스 #1과 같음. 링크만 다름 | 엔진이 위치를 모름 |
| A → A, G → G (교차 리전 · 계정 · 프로젝트) | 매트릭스 #5, #9와 같은 논리 복제 | 영수증, 펜싱, 역방향이 같아야 함 |
| 클라우드 자체 복제(교차 리전 읽기 복제본 승격, Aurora Global Database, Cloud SQL 교차 리전 복제본) | 쓰지 않음. 쓴다면 초기 복사 가속에만(확인 필요) | 승격은 클라우드 API라 Executor가 펜싱하지 못함. 역방향 경로가 없음(추론) |

리전 이전 자체는 설계 문서에만 둡니다(D-07).

### 3.2 칸별 증거 상태(2026-11-02 측정)

**측정**은 실제 클라우드에서 Executor 코드로 돌린 결과입니다. **막힘**은 시험으로 막힌 이유를 확인했습니다. **아직**은 돌리지 않았습니다. 증거 경로의 `R/`은 `deploy/pilot/runs/spike-rds-20261102T202459/`, `G/`는 `deploy/pilot/runs/spike-cloudsql-20261102T204821/`입니다(둘 다 gitignore).

공통 조건: C는 CNPG가 아니라 노트북의 PostgreSQL 16.15 컨테이너입니다(파일럿과 같은 공식 이미지). 링크는 임시 링크(D-16 전)입니다. A는 RDS for PostgreSQL 16.15(db.t4g.micro, 서울), G는 Cloud SQL for PostgreSQL 16.15(db-f1-micro, 도쿄)입니다. Aurora와 AlloyDB는 칸에 넣지 않았습니다.

| 출발 ↓ · 목적 → | A (RDS) | G (Cloud SQL) | C (자체 운영 PG16) |
|---|---|---|---|
| **A** | #5 **아직.** RDS 두 대를 잇는 실행은 하지 않았습니다. 부품은 측정했습니다. RDS 발행(#4), RDS 구독(#2) | #6 **아직.** 끝에서 끝 실행은 하지 않았습니다. 부품은 측정했습니다. RDS 발행(#4), Cloud SQL 구독(#3) | #4 **측정.** 정방향 10만 행 초기 복사 2.2초. 컷오버 4–8단계 통과. 4단계 294.8 ms(예산 400 ms). 영수증 MATCH 100,094행. 역방향 200행이 72.5 ms 뒤 RDS에 도착, 영수증 MATCH. 되돌리기(PG → RDS)도 MATCH. `R/cutover-rds-to-pg.json`, `R/failback-pg-to-rds.json` |
| **G** | #8 **아직.** 부품은 측정했습니다. Cloud SQL 발행(#7), RDS 구독(#2) | #9 **아직.** Cloud SQL 두 대를 잇는 실행은 하지 않았습니다 | #7 **측정.** 정방향 10만 행 초기 복사 12.5초(IAP 링크). 컷오버 4–8단계 통과. 영수증 MATCH 100,014행. 역방향 200행 892 ms 뒤 도착, 영수증 MATCH. 되돌리기 MATCH. 4단계는 1,607.6 ms로 예산 초과. 원인은 IAP 링크 왕복입니다(11.3절 4). `G/cutover-cloudsql-to-pg.json`, `G/failback-pg-to-cloudsql.json` |
| **C** | #2 **측정(부분).** RDS가 C를 구독하는 길을 측정했습니다. `executor_ctl`이 구독 생성(`origin = none`). 역방향 · 되돌리기에서 streaming, 영수증 MATCH. **구독자 TLS는 verify-full 불가**: CA 파일을 둘 수 없음. `sslrootcert=system`은 받지만 공개 CA 인증서 경로는 시험하지 않았습니다. 정방향 초기 복사(`copy_data = true`)를 RDS로 받는 실행은 아직입니다. `R/rev-sub.json` | #3 **측정(부분).** #2와 같은 범위입니다. Cloud SQL은 `sslrootcert=system`을 거부합니다(`certificate is not allowed`). 남는 길은 `require` + `channel_binding=require`뿐입니다. HA(REGIONAL)에서도 구독 생성, 동작, 수동 장애 조치 뒤 적용이 됐습니다(11.2절). `G/rev-sub.json`, `G/spk07-ha-failover.txt` | #1 **측정(파일럿, 이 스파이크 밖).** `docs/evidence/pilot-notebook/RUNS.tsv` |

**한 줄 요약.** 관리형 출발지에서 C로 가는 두 칸(#4, #7)은 컷오버와 되돌리기까지 측정으로 통과했습니다. 관리형이 구독자인 칸(#2, #3)은 동작하지만 구독자 쪽 이름 검증(verify-full)이 막혀 있습니다. 관리형끼리의 칸(#5, #6, #8, #9)은 아직입니다.

## 4. 관리형이 끼면 바뀌는 규칙

### 4.1 발행 쪽

- 발행은 지금처럼 테이블 목록(`FOR TABLE …`)으로 만듭니다. `bravo_fence`, `attachment_store`를 빼야 하기 때문입니다. `FOR ALL TABLES`는 superuser만 되므로(관찰 [P4]) 관리형에서 쓰지 않습니다. 테이블 목록 발행은 테이블 소유자면 됩니다(관찰 [P4]).
- 복제 연결 사용자 `replicator`: RDS · Aurora는 `rds_replication` GRANT(관찰 [A3]), Cloud SQL은 REPLICATION 속성(관찰 [G3])입니다.
- 슬롯 감시: RDS는 `OldestReplicationSlotLag`, `TransactionLogsDiskUsage` 지표가 있습니다. 슬롯을 읽지 않으면 저장 공간이 빨리 찬다고 경고합니다(관찰 [A13][A1]). 지금의 `wal_status` 감시(`03` 4.1)에 이 지표를 더합니다.
- `max_slot_wal_keep_size`는 Cloud SQL에서 플래그로 겁니다(관찰 [G1]). RDS · Aurora는 SPK-01에서 확인합니다. 못 걸면 지표 경보가 유일한 보호입니다.
- **고객 운영 DB 보호가 이전보다 먼저입니다.** 관리형 출발지는 고객의 운영 DB입니다. 구독자가 멈춘 동안 슬롯이 WAL을 붙잡으면 운영 DB의 저장 공간이 찹니다(관찰 [A1]). 논리 디코딩을 켜면 WAL 생성량도 늘 수 있습니다(관찰 [A1]). 그래서 슬롯 지연 경보에 기준(예: 남은 저장 공간 20%)을 두고, 기준을 넘으면 사람이 슬롯을 지우고 `03` 4.1의 재동기화로 갑니다(제안). Executor는 컷오버 밖에서 슬롯을 지우지 않습니다.

### 4.2 구독 쪽

| 구독자 | 누가 만드나 | 막는 것 |
|---|---|---|
| CNPG | 선언형 Subscription CRD(관찰 [N1]) | 관리형 publisher의 이름 검증(6절) |
| RDS · Aurora | `rds_superuser` 구성원(관찰 [A4][A5]). `pg_create_subscription`으로 좁힐 수 있는지 SPK-02 | 우리 publisher(CNPG)에 닿는 길과 CA |
| Cloud SQL | `cloudsqlsuperuser`(관찰 [G2]) | HA 불가(관찰 [G2]). `pg_subscription.subconninfo`를 읽을 수 없음(관찰 [G2]) → 재조회(`03` 4.2)는 `pg_stat_subscription`과 상태 열로만 |

**관리형 구독자가 우리 링크에 닿는 길(제안).** 관리형 DB는 쿠버네티스 ClusterIP를 풀지도, 닿지도 못합니다(추론). 그래서 같은 VPC 클러스터의 링크 Deployment(`link-<remote>-db`) 앞에 **내부 LB**(AWS 내부 NLB, GCP 내부 패스스루 LB)를 둡니다. 보안 그룹 · 방화벽은 그 DB에서 오는 5432만 허용합니다. PrivateLink나 VPC 피어링을 새로 만들지 않습니다(`02` 12절과 같은 방향). Cloud SQL PSA 인스턴스에서 고객 VPC의 내부 LB로 나가는 경로는 확인 필요입니다(SPK-07). EKS에서 내부 NLB를 만드는 방법(AWS Load Balancer Controller 또는 Service 주석)은 랜딩 존에 아직 없습니다(확인 필요).

- **내부 LB의 노출 범위.** 링크는 서비스 토큰을 가진 채 상대 DB로 TCP를 넘깁니다. 내부 LB에 닿는 누구나 그 토큰의 신원으로 상대 DB 앞까지 갑니다. 그래서 보안 그룹은 그 관리형 DB의 보안 그룹에서 오는 것만 허용합니다. Cloud SQL PSA는 출발 주소가 할당 대역 전체라 같은 대역의 다른 인스턴스도 닿습니다. 이 경우 남는 방어는 DB 인증(`replicator`, SCRAM)입니다(추론).

### 4.3 초기 복사

| 크기 | 방식 | 근거 |
|---|---|---|
| 기본(데모 10만 행) | `copy_data = true` | 지금 엔진 |
| 큼(복사 시간 동안 슬롯이 WAL을 너무 오래 붙잡음) | 슬롯을 만들며 스냅샷을 내보냄(`CREATE_REPLICATION_SLOT … (SNAPSHOT 'export')`) → `pg_dump --snapshot` → `pg_restore` → `copy_data = false, create_slot = false`로 구독 → `pg_replication_origin_advance`로 슬롯 시작 LSN 지정 → ENABLE | 조각은 관찰 [P7], 순서 조합은 추론. 관리형에서 되는지는 SPK-10 |
| Aurora → Aurora 같은 리전 | fast clone + `aurora_volume_logical_start_lsn()` + origin advance | 관찰 [A7] |
| CNPG → CNPG 같은 메이저 | PG17+ `pg_createsubscriber`(physical standby를 구독자로) | 관찰 [P7]. 지금 PG16이라 해당 없음 |

- 스냅샷 이름은 슬롯을 만든 세션이 다음 명령을 실행하거나 닫히면 무효입니다(관찰 [P7]). 그래서 이 세션은 덤프가 끝날 때까지 열어 둡니다.
- `pg_replication_origin_advance`는 기본으로 superuser만 됩니다(관찰 [P7]). Aurora 절차는 이 함수를 씁니다(관찰 [A7]). RDS · Cloud SQL은 SPK-10입니다.
- 어느 경우든 초기 복사 뒤 행 수, PK 해시, 내용 해시로 대조한 다음에만 컷오버로 갑니다(`03` 4.1 재동기화와 같은 규칙).

### 4.4 역방향 경로

- 목적지가 관리형이면 목적지가 publisher가 됩니다. 플랫폼 IaC가 처음부터 논리 디코딩을 켠 설정으로 만듭니다. 그래서 나중에 재시작할 일이 없습니다(추론).
- 출발지가 관리형이면 출발지가 구독자가 됩니다. 4.2절의 길과 CA 문제가 되돌리기의 전제입니다. 컷오버 1단계 전, 타이머 밖에서 역방향 구독을 `enabled = false`로 만들고 연결 시험(아래)을 통과해야 컷오버를 시작합니다.
- 역방향 연결 시험: 관리형 구독자에서 `ALTER SUBSCRIPTION … ENABLE` 없이 확인할 방법이 없습니다. 그래서 준비 단계에서 같은 conninfo로 빈 시험 발행 `bravo_probe`를 구독해 봅니다(추론, SPK-03).
- Cloud SQL HA 출발지(#7, #8, #9)는 역방향 구독자가 될 수 없습니다(관찰 [G2]). 고객이 컷오버 전에 HA를 끄는 데 동의하지 않으면 "데이터째 되돌리기"(FR-11)를 약속하지 않습니다. 이 경우 계획은 ANALYZE에서 NEEDS_HUMAN으로 갑니다(제안).

### 4.5 관리형에서 펜싱

`executor_ctl`은 superuser가 아닙니다. 관리형 마스터 사용자로 Executor를 돌리지 않습니다. 온보딩에서 마스터 사용자가 아래를 한 번 줍니다(추론. SPK-04로 확인).

| 펜싱 동작 | 필요한 것 | 근거 |
|---|---|---|
| `ALTER ROLE chat_app NOLOGIN` | `executor_ctl`에 CREATEROLE과 `chat_app`의 ADMIN OPTION. `GRANT chat_app TO executor_ctl WITH ADMIN OPTION, INHERIT FALSE, SET FALSE` | PG16 규칙: CREATEROLE은 ADMIN OPTION이 있는 비superuser · 비replication 역할만 바꿈(관찰 [P6]) |
| 기존 세션 종료 | `pg_signal_backend` 또는 대상 역할의 권한. INHERIT FALSE이므로 `pg_signal_backend`가 필요 | 관찰 [P5]. RDS `rds_superuser`(관찰 [A3]), Cloud SQL GRANT 절차(관찰 [G4]) |
| REVOKE(쓰기 권한 회수) | 권한을 준 역할(grantor)이나 소유자 | 추론. 소유자가 `executor_ctl`에 `WITH GRANT OPTION`으로 주고, `executor_ctl`이 `chat_app`에 줌. 그러면 회수도 `executor_ctl`이 함 |
| `bravo_fence` 갱신, barrier INSERT, `setval` | 테이블 · 시퀀스 권한 | 추론 |
| 구독 켜고 끄기(6a) | 구독 소유자 | 추론. 구독을 `executor_ctl` 소유로 만들 수 있는지 SPK-02 |
| 정방향 슬롯 삭제(6b) | publisher 쪽 REPLICATION 속성 또는 `rds_replication` | 관찰 [A3][G3] |

- 관리형에는 `pg_hba`가 없습니다. 평문 거부는 RDS `rds.force_ssl=1`(관찰 [A9]), Cloud SQL `ssl_mode`(확인 필요)로 대신합니다. N6 (b)는 이 설정으로 시험합니다.
- 우회 쓰기 확인(4단계)은 `pg_stat_activity`의 `usename`, `client_addr`로 합니다. 고객이 RDS Proxy를 쓰면 그 연결도 `chat_app` 세션이므로 NOLOGIN과 세션 종료에 걸립니다(추론). Proxy가 다시 붙지 못하므로 안전한 쪽으로 실패합니다.

### 4.6 시퀀스

PG18까지 시퀀스는 복제되지 않습니다(관찰 [P3]). 지금처럼 5단계에서 `setval`로 올립니다. PG19부터 `REFRESH SEQUENCES`가 생기지만 publisher가 PG19여야 합니다(관찰 [P3]). RDS의 19는 아직 preview입니다(관찰 [A12]). 그래서 이 설계는 `setval`을 유지합니다. CNPG 목적지에서는 `kubectl cnpg subscription sync-sequences`도 쓸 수 있습니다(관찰 [N1]).

### 4.7 호환성: 복제와 내용 해시를 깨는 것

| 위험 | 깨지는 것 | 대응 |
|---|---|---|
| 메이저가 다름 | 복제는 됨(관찰 [P8]). 타입의 텍스트 출력이 달라 내용 해시가 다를 수 있음(추론) | 플랫폼이 만드는 관리형 목적지는 출발지와 같은 메이저로 고정. 다르면 SPK-09 통과 뒤에만 |
| collation(glibc · ICU 판) | 판이 바뀌면 인덱스 손상 가능(관찰 [P9]). PK가 text면 `ORDER BY pk` 순서가 엔진마다 다를 수 있음(추론) | **해시 규칙 v1의 정렬을 `COLLATE "C"`로 고정**(제안, 바이트 순서). 복합 PK면 text 계열 열마다 붙임. ANALYZE에서 `datlocprovider`, `datcollate`, `datcollversion`을 양쪽 기록 |
| AlloyDB 로캘 | libc 로캘은 `C.UTF-8`, `en_US.utf8` 둘뿐(관찰 [G9]) | 다른 libc 로캘(예: `ja_JP.UTF-8`) 출발지는 같은 정렬을 재현하지 못함. 앱 정렬이 바뀜을 ANALYZE에 표시 |
| Cloud SQL 기본 collation | `en_US.UTF8`, psql로만 지정(관찰 [G8]) | 스키마 Job이 DB를 만들 때 출발지 값을 명시 |
| 세션 설정 | `TimeZone`, `DateStyle`, `IntervalStyle`, `extra_float_digits`, `bytea_output`가 다르면 텍스트 출력이 다름(추론) | 비교 세션에서 다섯 값을 고정해 해시 규칙 v1에 적음(제안) |
| 확장 | 목적지에 같은 확장 · 판이 없으면 스키마 Job 실패. 확장 타입의 텍스트 출력이 판마다 다를 수 있음(추론) | ANALYZE에서 `pg_extension`을 양쪽 비교. 확장 목록은 [A12], [G8] 페이지로 대조 |
| large object, DDL | 복제되지 않음(관찰 [P3]) | `pg_largeobject_metadata`가 비어 있지 않으면 BLOCK(제안). DDL은 지금처럼 전환 창 밖 |

## 5. 온보딩 단계

### 5.1 논리 디코딩 켜기

관리형 출발지는 켤 때 재시작이 필요합니다(2절). 그래서 컷오버와 따로, 고객의 정비 시간에 합니다.

| 순서 | 하는 일 | 누가 |
|---|---|---|
| 1 | 수집기가 현재 값을 읽음(`SHOW wal_level`, 파라미터 그룹 · 플래그, HA 여부) | 플랫폼(읽기 전용) |
| 2 | 재시작이 필요하다고 표시하고, 정비 시간과 함께 `change` 승인을 받음 | 운영자, 고객 |
| 3 | RDS: 사용자 파라미터 그룹을 만들고 `rds.logical_replication=1`. 기본 그룹은 수정할 수 없음(관찰 [A2]). 인스턴스에 붙이고 재부팅. Aurora: 클러스터 파라미터 그룹, writer 재부팅(관찰 [A2]). Cloud SQL: 플래그를 켜면 재시작(관찰 [G1]) | 고객 계정에 위임된 IaC 실행기, 또는 고객 |
| 4 | `SHOW wal_level`이 `logical`인지, 재시작 시간을 기록 | 플랫폼 |
| 5 | `replicator`에 복제 권한(4.1절), 슬롯 지표 경보 | 플랫폼 |

- 이 변경은 Executor의 효과가 아닙니다. Executor에는 클라우드 자격이 없습니다(`platform/executor/README.md`). 감사 기록에만 남깁니다.
- 기본 파라미터 그룹을 쓰던 인스턴스는 그룹을 바꾸는 것 자체가 재부팅을 부릅니다(추론).

### 5.2 PgBouncer 끼워 넣기

관리형 DB에는 우리가 쓸 PAUSE가 없습니다(1절). 그래서 앱과 관리형 DB 사이에 랜딩 존과 같은 PgBouncer(StatefulSet `pgbouncer-0`)를 넣습니다. 앱 연결 주소가 바뀌므로 첫 이전의 번역(`11-landing-zone.md` 5절)에 넣습니다.

| 순서 | 하는 일 | 확인 | 실패하면 |
|---|---|---|---|
| O1 | 사전 조사(읽기 전용): `pg_stat_activity`로 앱 연결 출처, `pg_locks`의 advisory 잠금, LISTEN, 임시 테이블, 200 ms를 넘는 트랜잭션, RDS Proxy 사용 여부 | transaction 풀링과 맞지 않는 기능 목록 | 맞지 않으면 계획을 PROPOSE로. 앱 수정 제안 |
| O2 | 같은 VPC의 고객 클러스터에 PgBouncer 배포. `[databases]`는 관리형 엔드포인트 이름. 서버 쪽 `verify-full`과 공급자 CA 번들 | `SHOW SERVERS`에 TLS 연결 | 배포 취소 |
| O3 | 인증: 앱 비밀번호를 금고에서 `auth_file`로. 관리형은 `pg_shadow`를 읽을 수 없어 `auth_query`를 못 쓴다고 봄(추론, SPK-05) | 앱 역할로 PgBouncer 경유 로그인 | O2로 |
| O4 | 합성 부하로 PgBouncer 경유 지연과 PAUSE · RESUME 왕복 측정 | N11 합격선(PAUSE 왕복 p95 ≤ 300 ms) | 원인 보고, 다음 단계로 가지 않음 |
| O5 | 앱의 DB 주소를 `pgbouncer.<ns>.svc:6432`로 바꿈. 비밀 주입(Secret) 변경 → 롤링 재시작. `change` 승인 | 앱 정상 | 이전 주소로 되돌림 |
| O6 | 직접 연결이 남지 않았는지 확인: `chat_app` 세션의 `client_addr`가 모두 PgBouncer Pod IP | 직접 세션 0 | 남은 출처를 보고. 컷오버 4단계 펜싱이 마지막 방어 |
| O7 | (선택) 관리형 DB 5432를 PgBouncer, `cf-edge`, 링크 내부 LB에서만 받게 보안 그룹을 좁힘 | 직접 접속 거부 | 고객 결정 |

- RDS Proxy는 앱 경로에서 뺍니다. PgBouncer를 RDS Proxy 앞에 두면 계층이 하나 늘고 Proxy가 연결을 따로 잡습니다(추론).
- **범위.** 이 단계는 출발지 앱이 관리형 DB와 같은 VPC의 쿠버네티스에서 돌 때만 됩니다. 앱이 VM이나 서버리스에 있으면 PgBouncer를 둘 자리와 연결 주소 변경 방식이 달라지므로 이 설계의 범위 밖입니다(제안).
- PgBouncer가 하나뿐인 출발지 경로가 고객의 가용성 요구를 못 맞추면, replica를 늘리는 대신 Pod별 PAUSE 효과 행을 늘려야 합니다(`03` 4.1). P0 범위는 replica 1입니다.

## 6. TLS와 DNS: 링크 위에서도 verify-full

**문제.** 링크는 상대 DB를 `src-db.bravo-link.svc.cluster.local` 같은 우리 이름으로 보입니다. 관리형 서버 인증서에는 공급자 이름만 있습니다(RDS, 관찰 [A9]). RDS에서 우리 이름을 넣는 방법은 문서에서 찾지 못했습니다. 링크는 TCP를 그대로 넘기므로 TLS는 끝에서 끝입니다. 그래서 클라이언트가 **공급자 이름으로 검증하고, TCP는 링크로 보내면** 됩니다.

| 클라이언트(위치) | 서버 | 검증 이름 | TCP 경로 | CA | 상태 |
|---|---|---|---|---|---|
| 출발지 PgBouncer(같은 VPC) | RDS · Cloud SQL | 공급자 엔드포인트 | 직접 | 공급자 CA 번들 | 관찰 [A9] |
| Executor, 수집기(libpq) | 원격 관리형 | 공급자 엔드포인트(`host=`) | `hostaddr=` 링크 Service IP. 실행 때 kube-dns로 풀어 넣음 | 공급자 CA | libpq는 `hostaddr`로 접속하고 `host`로 검증(관찰 [P10]) |
| CNPG 구독자 | 원격 관리형 publisher | 공급자 엔드포인트 | `hostaddr=` 링크 Service의 **고정 ClusterIP** | 공급자 CA를 `/projected/peer-ca/ca.crt`로 | externalClusters에 `hostaddr`를 넣을 수 있는지 확인 필요(SPK-06) |
| 원격 PgBouncer(SHIFT 중 목적지 PgBouncer) | 원격 관리형 | 공급자 엔드포인트 | Pod `hostAliases`가 공급자 이름을 고정 ClusterIP로 | 공급자 CA를 `server_tls_ca_file`에 더함 | PgBouncer에는 `hostaddr` 같은 옵션을 문서에서 찾지 못함([B1]). `/etc/hosts`를 읽는지 확인 필요(SPK-05) |
| 관리형 구독자 | 원격 CNPG publisher | 우리가 가진 공개 도메인 이름(예: `src-db.link.<도메인>`) | `hostaddr=` 내부 LB IP | 공개 CA 인증서 + `sslrootcert=system` | 관리형 서버가 시스템 신뢰 저장소를 쓰는지 확인 필요(SPK-03) |
| 관리형 구독자(위가 안 될 때) | 원격 CNPG publisher | 없음 | 같음 | `sslmode=require` + `channel_binding=require`(SCRAM) | 추론. 이름 검증이 아니므로 ENC-01 예외로 보안 검토 필요 |
| 누구든 | AlloyDB | - | Auth Proxy | IAM | 직접 verify-full 불가(관찰 [G10]). 보류 |

- **고정 ClusterIP.** 링크 Service `src-db`에 `spec.clusterIP`를 정해 둡니다. CNPG conninfo와 `hostAliases`가 IP를 담기 때문입니다(추론).
- **공개 CA 인증서의 비용.** 관리형 구독자용 첫 안(`sslrootcert=system`)은 우리 CNPG publisher가 공개 CA 인증서를 가져야 합니다. 그러려면 우리가 가진 도메인의 이름, ACME 발급과 갱신(cert-manager DNS-01 등), CNPG의 사용자 서버 인증서 설정이 필요합니다. `02` 12절은 cert-manager를 넣지 않는다고 정했으므로, 이 안을 채택하면 그 줄이 바뀝니다(제안). SAN에는 공개 이름과 기존 링크 이름을 모두 넣습니다.
- **Cloud SQL 이름.** 기본 CA 모드(인스턴스별 CA)에서 PSA 인스턴스의 인증서에는 DNS SAN이 없습니다(관찰 [G5]). 그러면 이름 검증이 안 됩니다. 플랫폼이 만드는 Cloud SQL은 공유 CA 모드(`GOOGLE_MANAGED_CAS_CA`)로 만듭니다. 고객 CA 모드(`CUSTOMER_MANAGED_CAS_CA`)면 사용자 SAN에 링크 이름을 직접 넣을 수 있어 DNS 재정의가 필요 없습니다(관찰 [G5]). 고객의 기존 인스턴스가 인스턴스별 CA면 CA 모드 변경이 온보딩에 들어갑니다(변경 가능 여부와 영향은 확인 필요, SPK-07).
- **Aurora.** writer 클러스터 엔드포인트를 검증 이름으로 씁니다. SAN을 지원하는 클라이언트가 필요합니다(관찰 [A9]). libpq는 SAN을 먼저 봅니다(관찰 [P10]).
- **N6 확장.** 관리형 끝마다 N6 (a)–(d)를 같은 방식으로 돌립니다. (d)는 공급자 이름이 아닌 이름으로 접속하면 거부되는지입니다.

## 7. 플랫폼 IaC가 만드는 관리형 목적지와 비용

플랫폼 IaC는 `infra/modules/database/` 아래에 CNPG 구현과 같은 계약으로 두 구현을 더합니다. 이 문서는 계약만 적고 `infra/`는 고치지 않습니다.

| 모듈(제안) | 만드는 것 |
|---|---|
| `database/rds` | 사설 서브넷의 DB 서브넷 그룹. 보안 그룹(5432는 PgBouncer, `cf-edge`, 링크 내부 LB에서만). 사용자 파라미터 그룹(`rds.logical_replication=1`, `rds.force_ssl=1`, `wal_sender_timeout`, 가능하면 `max_slot_wal_keep_size`). KMS 암호화, 삭제 보호, 출발지와 같은 메이저 |
| `database/cloudsql` | PSA 사설 IP 인스턴스. 공유 CA 모드. 플래그 `cloudsql.logical_decoding=on`, `max_slot_wal_keep_size`. 이전 중에는 ZONAL(구독자가 되어야 하므로). HA(REGIONAL)는 DONE 뒤 역방향을 끈 다음 사람이 정함 |
| 클러스터 쪽(랜딩 존) | PgBouncer StatefulSet, 공급자 CA 번들 Secret, 링크 내부 LB Service, `cf-edge`의 관리형 엔드포인트 라우트 |

- 마스터 자격은 Terraform 상태에 남기지 않습니다. 어디에 둘지(클라우드 관리형 비밀 또는 우리 금고)는 인프라 · CI 담당이 정합니다(열림).
- **비용.** 관리형 인스턴스 시간 요금, 저장소, 백업, HA면 약 두 배, 내부 LB 시간 요금, 고객 CA 모드면 CA Service 요금이 듭니다. 금액은 채택할 때 공식 요금표로 확인합니다(확인 필요). 데모에서는 비용 승인 전에 관리형 목적지를 만들지 않습니다(`11-landing-zone.md` 8절).

## 8. 랜딩 존에 주는 영향(제안)

| 항목 | 제안 |
|---|---|
| DB 백엔드 선택 | 환경마다 `db_backend` = `cnpg`(기본) 또는 `managed`. `managed`는 EKS에서 RDS · Aurora, GKE에서 Cloud SQL. AlloyDB는 보류 |
| 앱이 보는 것 | 어느 쪽이든 같은 PgBouncer 주소, 같은 Secret 이름, 같은 PostgreSQL 메이저. 앱 경험이 같음 |
| 같은 계약 시험 | 역할(`replicator`, `chat_app`, `executor_ctl`), `bravo_fence`, 테이블 목록 발행, 모든 테이블 PK, verify-full(N6), PAUSE 왕복(N11) |
| D-23과의 관계 | D-23은 RDS를 목적지 구성요소로 쓰지 않는다고 정했습니다. 이 제안은 DB에 한해 고객이 원할 때만 예외를 둡니다. 팀장 결정이 필요합니다 |

## 9. 관리형 이전 서비스(DMS)는 언제 쓰나

**쓰는 때(정방향만).**
- publisher가 PG10 미만이라 native 논리 복제가 안 될 때. CNPG 문서는 publisher PG10 이상을 요구합니다(관찰 [N1]). Google DMS는 pglogical로 옮깁니다(관찰 [G11]). 지원하는 원본 판은 확인 필요입니다.
- 목적지가 AlloyDB이고 SPK-11이 실패했을 때. Google DMS가 문서화된 경로입니다(관찰 [G11]).

**DMS가 풀지 못하는 것.** 논리 디코딩 켜기와 재시작은 DMS에도 똑같이 필요합니다. AWS DMS는 `rds.logical_replication=1`(관찰 [A11]), Google DMS는 pglogical과 `wal_level=logical`(관찰 [G11])이 필요합니다.

**우리 모델과 맞지 않는 이유.**

| 우리 모델 | DMS |
|---|---|
| 펜싱: 자원 쪽 펜스(`bravo_fence`)와 효과 원장 | 작업 시작 · 중지 · promote가 클라우드 API입니다. Executor는 클라우드 자격이 없고, 자원 쪽 펜스가 없어 효과가 `UNKNOWN`으로 남습니다(추론) |
| 목적지는 컷오버 전 쓰기 금지 | Google DMS 목적지는 이전 중에도 쓰기가 됩니다(관찰 [G11]). 막는 일은 결국 우리 펜싱입니다 |
| 되돌리기: 미리 만든 역방향 구독(`origin = none`) | AWS DMS 동종 이전의 목적지는 RDS · Aurora뿐(관찰 [A11]). Google DMS 목적지는 Cloud SQL · AlloyDB뿐(관찰 [G11]). 그래서 CNPG나 다른 클라우드로 되돌리는 길이 없습니다. 일반 AWS DMS 작업으로 되돌리는 경로와 양방향 루프 방지는 확인하지 않았습니다(확인 필요). 어느 경우든 역방향 작업도 클라우드 API라 첫 행과 같은 펜싱 문제가 남습니다 |
| 시퀀스는 5단계에서 올림 | 두 DMS 모두 CDC 중 시퀀스를 맞추지 않습니다(관찰 [A11][G11]). 우리 `setval`이 여전히 필요합니다 |
| 영수증: 해시 규칙 v1 | AWS DMS 검증은 행 단위 비교입니다(관찰 [A11]). 영수증은 우리 최종 비교로 따로 만들어야 합니다 |
| 객체 범위 | AWS DMS는 OID LOB를 옮기지 않고, PK가 없으면 UPDATE · DELETE를 무시합니다(관찰 [A11]). Google DMS는 Large Object를 지원하지 않습니다(관찰 [G11]) |
| 백업 | Google DMS는 promote 전까지 자동 백업과 PITR이 멈춥니다(관찰 [G11]) |

**결론(제안).** DMS는 native가 막힌 정방향에만 씁니다. 이때도 펜싱, barrier, 최종 비교, 영수증은 우리 것을 씁니다. 되돌리기가 데이터째가 아니므로 계획은 NEEDS_HUMAN으로 가고, 고객의 명시적 수용이 있어야 진행합니다.

## 10. 막는 것 상위 다섯

| 순위 | 막는 것 | 근거 | 영향 받는 방향 | 대응 |
|---|---|---|---|---|
| 1 | 관리형 구독자가 우리 publisher를 이름으로 검증하지 못함. **길은 열림** | **측정(SPK-03)**. 두 엔진 모두 CA 파일을 둘 수 없음(`root.crt does not exist`). RDS는 `sslrootcert=system`을 받고 시스템 신뢰 저장소로 검증함. Cloud SQL은 `sslrootcert=system`을 거부함(`certificate is not allowed`). 두 엔진 모두 `require` + `channel_binding=require`는 동작. 같은 VPC의 링크 끝(RDS → Pod IP, Cloud SQL PSA → VM IP)으로 가는 길은 동작 | A → C의 되돌리기, C → A 정방향, #5–#9 | Cloud SQL은 `channel_binding=require`밖에 없음. 그래서 ENC-01 예외의 보안 검토가 이 설계의 전제가 됨(제안). RDS만 공개 CA 인증서 안이 남음(시험 안 함) |
| 2 | Cloud SQL HA 인스턴스의 구독자 제한 | 관찰 [G2]는 "HA는 나가는 IP가 일정하지 않아 구독자가 될 수 없다"고 적음. **측정(SPK-07)**: REGIONAL에서 구독 생성 · ENABLE · 수동 장애 조치(23초) 뒤 적용이 모두 됨 | G 출발지의 되돌리기(#7–#9), G 목적지(#3, #6) | 막는 것은 기능이 아니라 출발 IP입니다. publisher 쪽 허용을 PSA 할당 대역 전체로 두면 됩니다(측정 환경이 그렇게 함). 지원 범위 밖이므로 기본은 여전히 ZONAL로 두고, HA 출발지는 사람 확인으로 진행(제안) |
| 3 | AlloyDB 직접 연결은 verify-full 불가, 복제 역할을 줄 수 없음, 외부 native 구독 절차 없음 | 관찰 [G9][G10]. **측정(SPK-11, TLS만)**: 직접 연결 인증서가 자체 서명(`CN=unused`, SAN `localhost`)이라 공식 CA로 verify-ca · verify-full 모두 실패. `require`(암호화만)는 동작 | AlloyDB 전부 | 보류. Google 관리형은 Cloud SQL. 복제 역할과 구독 시험은 아직 |
| 4 | 관리형 출발지의 논리 디코딩은 재시작이 필요 | 관찰 [A1][A2][G1]. **측정(SPK-01)**: RDS는 클라이언트 단절 11.8초(API 66.5초), Cloud SQL은 34.4초(작업 51.5초) | A · G 출발지 전부 | 5.1 온보딩. 정비 시간과 `change` 승인. 컷오버 일정보다 먼저 |
| 5 | 관리형에는 PAUSE가 없고, PgBouncer는 공급자 이름 검증에 `hostaddr`를 못 씀 | 관찰 [A8][G7]. **측정(SPK-05)**: `/etc/hosts` 재정의로 RDS · Cloud SQL 모두 verify-full 통과, 틀린 이름 거부. Cloud SQL PSA 인증서 SAN은 끝에 점이 있어 `host=`와 hosts 항목 모두 점까지 써야 함 | 관리형 끝이 있는 모든 방향 | 5.2 온보딩 + `hostAliases`와 고정 ClusterIP. Cloud SQL은 끝 점 이름을 쿠버네티스 `hostAliases`가 받는지 확인 필요. 안 되면 사설 DNS 레코드(PSA 이름은 `recordManager: CUSTOMER`) |

다음 순위: Aurora writer 장애 조치 때 논리 슬롯 유지(SPK-08, 아직). PG16 ADMIN OPTION 규칙에 맞춘 펜싱 권한(SPK-04)은 RDS · Cloud SQL 모두 측정으로 통과했습니다. RDS의 `max_slot_wal_keep_size`(SPK-01)는 2048 MB가 걸립니다. Cloud SQL은 이 플래그가 102400 MB(100 GB) 미만을 거부하므로, 작은 인스턴스에서는 보호가 되지 않습니다(측정, 11.2절).

## 11. 스파이크 시험

먼저 돌릴 것은 SPK-03, SPK-02, SPK-04, SPK-05, SPK-09입니다. SPK-09는 컨테이너만으로 시작할 수 있습니다. 관리형 인스턴스를 만드는 시험은 비용 승인 뒤에 합니다(`AGENTS.md`).

| ID | 확인할 것 | 합격 기준 |
|---|---|---|
| SPK-01 | RDS 논리 디코딩 켜기, 재부팅 시간, `max_slot_wal_keep_size` | `wal_level=logical`, 재부팅 시간 기록, 상한이 걸림 |
| SPK-02 | RDS 구독자에서 `pg_create_subscription`, `origin = none`, origin advance | 세 가지가 `executor_ctl`로 됨. 안 되면 필요한 최소 역할을 기록 |
| SPK-03 | 관리형 구독자(RDS, Cloud SQL)가 내부 LB 링크로 CNPG publisher에 verify-full로 붙는지 | `sslrootcert=system` + 공개 CA 이름으로 구독이 `streaming`. 실패면 `channel_binding` 경로 결과 기록 |
| SPK-04 | RDS · Cloud SQL에서 `executor_ctl`의 펜싱과 시간 | NOLOGIN, REVOKE, 세션 종료가 되고 앱 쓰기가 거부됨. 400 ms 예산 안 |
| SPK-05 | PgBouncer 끼워 넣기, `hostAliases` 재정의, 원격 PAUSE 왕복, `auth_file` 인증 | 공급자 이름으로 verify-full 통과, 틀린 이름은 실패, PAUSE p95 ≤ 300 ms |
| SPK-06 | CNPG externalClusters의 `hostaddr`로 RDS publisher 구독 | 구독 `streaming`, N6 (d) 거부 |
| SPK-07 | Cloud SQL CA 모드와 인증서 SAN, HA 구독 거부, PSA에서 내부 LB로 나가는 길 | 공유 CA 모드에 DNS SAN. HA에서 구독 실패를 재현 |
| SPK-08 | Aurora writer 장애 조치 뒤 논리 슬롯 | 슬롯 유지 여부 기록. 잃으면 4.1 재동기화 경로로 |
| SPK-09 | 해시 규칙 v1의 엔진 간 안정성 | 같은 데이터에서 `COLLATE "C"` 정렬 해시가 CNPG · RDS · Cloud SQL에서 같음 |
| SPK-10 | 관리형 publisher에서 스냅샷 내보내기 + 따라잡기 | 대조 일치, origin advance 성공 |
| SPK-11 | AlloyDB 복제 사용자와 외부 publisher 구독, Auth Proxy 경유 복제 연결 | 하나라도 실패하면 AlloyDB 보류 유지 |

**명령.** `<…>`는 환경 값입니다. 비밀번호는 금고에서 환경 변수로 넣고 명령줄에 쓰지 않습니다.

```bash
# SPK-01 RDS 논리 디코딩
aws rds create-db-parameter-group --db-parameter-group-name bravo-pg16-logical \
  --db-parameter-group-family postgres16 --description "bravo logical replication"
aws rds modify-db-parameter-group --db-parameter-group-name bravo-pg16-logical --parameters \
  "ParameterName=rds.logical_replication,ParameterValue=1,ApplyMethod=pending-reboot" \
  "ParameterName=max_slot_wal_keep_size,ParameterValue=10240,ApplyMethod=pending-reboot"
aws rds modify-db-instance --db-instance-identifier <id> \
  --db-parameter-group-name bravo-pg16-logical --apply-immediately
time aws rds reboot-db-instance --db-instance-identifier <id>
psql "$RDS_URL" -c "SHOW wal_level" -c "SHOW max_slot_wal_keep_size"
```

```sql
-- SPK-02 RDS 구독자 (마스터 사용자로 권한, executor_ctl로 생성)
GRANT pg_create_subscription TO executor_ctl;          -- 거부되면 오류 기록
SET ROLE executor_ctl;
CREATE SUBSCRIPTION rev_sub
  CONNECTION 'host=<publisher 이름> hostaddr=<내부 LB IP> dbname=chat user=replicator sslmode=verify-full sslrootcert=system password=…'
  PUBLICATION pub_dst
  WITH (enabled = false, copy_data = false, origin = none);
SELECT subname, subenabled, suborigin, subowner::regrole FROM pg_subscription;
SELECT pg_replication_origin_advance('pg_' || oid, '0/0') FROM pg_subscription WHERE subname = 'rev_sub';
```

```sql
-- SPK-03 관리형 구독자의 TLS (RDS와 Cloud SQL에서 각각)
-- (a) 공개 CA 인증서 이름 + 시스템 신뢰 저장소
CREATE SUBSCRIPTION probe_a CONNECTION 'host=src-db.link.<도메인> hostaddr=<내부 LB IP> dbname=chat user=replicator sslmode=verify-full sslrootcert=system password=…' PUBLICATION bravo_probe;
-- (b) 이름 검증 없이 채널 바인딩
CREATE SUBSCRIPTION probe_b CONNECTION 'host=<내부 LB IP> dbname=chat user=replicator sslmode=require channel_binding=require password=…' PUBLICATION bravo_probe;
SELECT subname, pid IS NOT NULL AS running, latest_end_lsn FROM pg_stat_subscription;
-- 공급자 로그에서 TLS 오류 확인(RDS: describe-db-log-files, Cloud SQL: Cloud Logging)
```

```sql
-- SPK-04 펜싱 권한과 시간 (마스터 사용자가 먼저 GRANT)
ALTER ROLE executor_ctl CREATEROLE;                    -- 관리형에서 거부되면 기록
GRANT chat_app TO executor_ctl WITH ADMIN OPTION, INHERIT FALSE, SET FALSE;
GRANT pg_signal_backend TO executor_ctl;
-- executor_ctl로
\timing on
BEGIN;
UPDATE bravo_fence SET epoch = 2 WHERE epoch <= 2;
ALTER ROLE chat_app NOLOGIN;
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM chat_app;
COMMIT;
SELECT pg_terminate_backend(pid, 1000) FROM pg_stat_activity WHERE usename = 'chat_app';
-- chat_app으로 INSERT 시도 → 거부 확인
```

```bash
# SPK-05 PgBouncer 끼워 넣기와 이름 재정의
kubectl -n bravo-link get svc src-db -o jsonpath='{.spec.clusterIP}'
# pgbouncer StatefulSet: hostAliases: [{ip: <위 IP>, hostnames: [<rds 엔드포인트>]}]
kubectl -n chat exec pgbouncer-0 -- getent hosts <rds 엔드포인트>
psql "host=pgbouncer.chat.svc port=6432 dbname=chat user=chat_app sslmode=require" \
  -c "select inet_server_addr(), current_setting('server_version')"
for i in $(seq 20); do
  psql "host=pgbouncer.chat.svc port=6432 dbname=pgbouncer user=pgb_admin sslmode=require" \
    -c "\timing on" -c "PAUSE chat" -c "RESUME chat"
done
```

```yaml
# SPK-06 CNPG 구독자 → RDS publisher (externalClusters에 hostaddr)
externalClusters:
  - name: src-rds
    connectionParameters:
      host: <rds 엔드포인트>
      hostaddr: <src-db 링크 고정 ClusterIP>
      dbname: chat
      user: replicator
      sslmode: verify-full
      sslrootcert: /projected/peer-ca/ca.crt
```

```bash
# SPK-07 Cloud SQL CA 모드와 SAN, HA 구독 거부
gcloud sql instances create bravo-spk --database-version=POSTGRES_16 --region=asia-northeast1 \
  --tier=db-custom-1-3840 --network=<vpc> --no-assign-ip --server-ca-mode=GOOGLE_MANAGED_CAS_CA \
  --database-flags=cloudsql.logical_decoding=on,max_slot_wal_keep_size=10240
gcloud sql instances describe bravo-spk --format='value(dnsName,settings.ipConfiguration.serverCaMode)'
openssl s_client -starttls postgres -connect <사설 IP>:5432 </dev/null 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName
gcloud sql instances patch bravo-spk --availability-type=REGIONAL
# 이어서 SPK-03의 CREATE SUBSCRIPTION → 실패 메시지 기록
```

```bash
# SPK-08 Aurora writer 장애 조치와 논리 슬롯
psql "$AURORA_WRITER" -c "select slot_name, active, wal_status, confirmed_flush_lsn from pg_replication_slots"
aws rds failover-db-cluster --db-cluster-identifier <id>
psql "$AURORA_WRITER" -c "select slot_name, active, wal_status, confirmed_flush_lsn from pg_replication_slots"
```

```sql
-- SPK-09 해시 규칙 안정성 (CNPG 16, RDS 16, Cloud SQL 16에서 같은 데이터로)
-- 데이터: 한글 · 대소문자 · 공백이 섞인 text PK, timestamptz, float8, numeric, jsonb, bytea
SET TimeZone = 'UTC'; SET DateStyle = 'ISO'; SET IntervalStyle = 'postgres';
SET extra_float_digits = 3; SET bytea_output = 'hex';
SELECT md5(string_agg(t::text, '|' ORDER BY id COLLATE "C")) AS c_hash,
       md5(string_agg(t::text, '|' ORDER BY id))             AS db_hash
FROM messages_text_pk t;
SELECT datname, datlocprovider, datcollate, datcollversion FROM pg_database WHERE datname = current_database();
SELECT extname, extversion FROM pg_extension ORDER BY 1;
```

```text
SPK-10 큰 DB 초기 복사 (관리형 publisher)
1. 세션 A(열어 둠): psql "host=<pub> dbname=chat replication=database user=replicator sslmode=verify-full"
   CREATE_REPLICATION_SLOT fwd_slot LOGICAL pgoutput (SNAPSHOT 'export');   -- consistent_point, snapshot_name 기록
2. 다른 셸: pg_dump --snapshot=<snapshot_name> -Fd -j 4 -d "$PUB_URL" -f /dump
            pg_restore -j 4 -d "$SUB_URL" /dump
3. 세션 A 닫기
4. 구독자: CREATE SUBSCRIPTION fwd … WITH (copy_data = false, create_slot = false, slot_name = 'fwd_slot', enabled = false);
           SELECT pg_replication_origin_advance('pg_' || oid, '<consistent_point>') FROM pg_subscription WHERE subname = 'fwd';
           ALTER SUBSCRIPTION fwd ENABLE;
5. 행 수 · PK 해시 · 내용 해시 대조
```

```sql
-- SPK-11 AlloyDB
SELECT rolname, rolsuper, rolreplication, rolcreaterole FROM pg_roles
WHERE rolname IN ('postgres', 'alloydbsuperuser');
-- 이어서 SPK-03과 같은 CREATE SUBSCRIPTION, Auth Proxy 경유 replication=database 접속 시도
```

### 11.1 스파이크 결과: RDS(측정, 2026-11-02)

**환경.** RDS for PostgreSQL 16.15, db.t4g.micro, 단일 AZ, gp3 20 GiB, 자동 백업 없음, 서울. 출발지 VPC의 사설 서브넷에 두었습니다(`infra/envs/spike-db`). C 쪽은 노트북의 PostgreSQL 16.15 컨테이너입니다. 데이터는 `messages` 10만 행(한글, 대소문자, 탭, `|`, 줄바꿈, 이모지), `attachments` 20행(uuid PK), `message_attachments` 20행입니다. Executor 코드(`ChatDb`, `fenced`, `TableDigest`, `PgBouncerAdmin`)는 고치지 않고 그대로 불렀습니다(`deploy/pilot/spike-db/spike.py`).
- 정방향 길: 노트북 → `kubectl port-forward` → EKS 안 TCP 중계 Pod → RDS. TLS는 끝에서 끝입니다.
- 역방향 길: RDS → 같은 VPC의 중계 Pod IP(`tools/link` relay) → 노트북이 미리 연 연결 → 노트북 PG.

| ID | 결과 | 측정값 | 증거 |
|---|---|---|---|
| SPK-01 | 통과 | 재부팅 전 `wal_level=minimal`(자동 백업 0일), 뒤 `logical`. 재부팅 호출부터 클라이언트 단절 11.8초(9.3초에 끊기고 20.6초에 복귀). API `available`까지 66.5초. `max_slot_wal_keep_size`는 dynamic이고 2048 MB가 걸림(슬롯 `safe_wal_size` 값 있음) | `R/spk01-summary.json`, `R/spk01-probe.jsonl`, `R/rds-parameters.json` |
| SPK-02 | 통과(조건부) | 비superuser `executor_ctl`(`pg_create_subscription`, DB CREATE)이 RDS에서 구독을 만듦(`origin = none`, `enabled = false`, `create_slot = false`). ENABLE · DISABLE, 정방향 슬롯 삭제(`rds_replication`)도 됨. **`pg_replication_origin_advance`는 `executor_ctl`에 거부.** 마스터는 실행할 수 있지만 EXECUTE를 다른 역할에 GRANT하지 못함 | `R/rev-sub.json`, `R/origin-advance-master.txt`, `R/privileges.json` |
| SPK-03 | 부분 | CA 파일 지정 불가(`/root/.postgresql/root.crt does not exist`). `sslrootcert=system`은 받고 시스템 저장소로 검증(사설 CA에 `CERTIFICATE_VERIFY_FAILED`). `require` + `channel_binding=require` 성공. 공개 CA 인증서 경로는 시험하지 않음 | `R/rev-sub.json` |
| SPK-04 | 통과 | 4단계(NOLOGIN, REVOKE, 세션 종료) 294.8 ms. REVOKE commit 뒤 성공한 앱 쓰기 0건. 옛 epoch 명령은 RDS `bravo_fence`가 거부(E5) | `R/cutover-rds-to-pg.json` |
| SPK-05 | 통과(PAUSE 주의) | 파일럿 PgBouncer 1.26.1이 `/etc/hosts`(Pod `hostAliases`와 같은 방식)로 RDS 이름을 링크로 보내고 verify-full 통과(TLSv1.3). 틀린 이름은 `name … not present in server certificate`로 거부. `auth_file` SCRAM 로그인 성공. `auth_query`는 불가(`pg_shadow` 권한 없음). PAUSE+RESUME 왕복 p95 6.1 ms(20회, 쓰기 4개). **같은 부하에서 쓰기 679건 중 406건이 `server login has been failing, cached error: pause mode`로 실패**(11.3절 3) | `R/spk05-pgbouncer.json`, `R/spk05-pgbouncer.log` |
| SPK-06 | 부분 | CNPG가 아니라 일반 PG 구독자로 시험. `host=<RDS 이름> hostaddr=<링크 주소>` + verify-full로 streaming. 틀린 이름, IP 문자열, 틀린 CA, 평문은 구독 생성에서 모두 거부(N6 d). CNPG `externalClusters`의 `hostaddr`는 아직 | `R/fwd-sub.json`, `R/n6-tls-client.txt` |
| SPK-09 | 통과 | 해시 규칙 v1.1 영수증 MATCH(10만 행, uuid PK 포함). 세션 `TimeZone=Asia/Tokyo`, `DateStyle='ISO, DMY'`에서도 같음. **같은 `en_US.UTF-8`인데 glibc 판(RDS 2.26-59.amzn2, Debian 2.41) 때문에 기본 collation 정렬이 다름.** `COLLATE "C"` 해시는 같음 | `R/digest-after-initial-copy.json`, `R/spk09.json` |

**스키마가 RDS에서 막히는 곳(측정).** 마스터는 `rds_superuser`이고 REPLICATION 속성이 없습니다.
- `CREATE ROLE … REPLICATION`과 `ALTER ROLE … REPLICATION`은 거부됩니다(PG16: REPLICATION 역할만 REPLICATION 역할을 만듦). `GRANT rds_replication`으로 바꿉니다. 이 스파이크의 변형은 두 줄입니다(`deploy/pilot/spike-db/rds_02_roles.sql`, `R/schema-variant.diff`). 이 변형은 당시 판의 `02_roles.sql`(평문 비밀번호 변수) 기준입니다.
- `pg_hba_file_rules` GRANT는 거부됩니다. 마스터는 읽을 수 있지만 남에게 줄 수 없습니다. DB-AUTH 입력은 RDS에서 파라미터 그룹(`rds.force_ssl`) 조회로 바꿔야 합니다(제안). 평문 접속은 `no pg_hba.conf entry … no encryption`으로 거부됨을 확인했습니다.
- `bravo_catalog.role_has_password()`는 `pg_authid` 권한이 없어 실패합니다(마스터도 같음). RDS에서는 이 입력을 "확인 불가"로 기록해야 합니다(제안).

### 11.2 스파이크 결과: Cloud SQL(측정, 2026-11-02)

**환경.** Cloud SQL for PostgreSQL 16.15, Enterprise, db-f1-micro, HDD 10 GiB, 도쿄, PSA 사설 IP만, `ssl_mode = ENCRYPTED_ONLY`, 공유 CA 모드(`GOOGLE_MANAGED_CAS_CA`). 프로젝트 `b-free-target-20261102`(`infra/envs/spike-db/gcp`). 링크는 같은 VPC의 e2-micro VM(외부 IP 없음)이고, 노트북은 IAP TCP 터널로 닿습니다. 정방향은 VM이 Cloud SQL로 TCP를 넘기고, 역방향은 같은 relay가 VM에서 돕니다.

| ID | 결과 | 측정값 | 증거 |
|---|---|---|---|
| SPK-01(G) | 통과 | `cloudsql.logical_decoding=on`은 재시작함(postmaster 시작 시각이 바뀜). 클라이언트 단절 34.4초, 작업 51.5초. `log_min_error_statement` 변경은 재시작하지 않음. **`max_slot_wal_keep_size`는 102400–10485760 MB만 받음.** 2048은 인스턴스 생성 자체를 실패시킴. 10 GiB 디스크에서 100 GB 상한은 보호가 아님 | `G/spk01-summary.json`, `G/spk01-cloudsql-max-slot-wal-keep-size-rejected.txt` |
| SPK-02(G) | 통과(조건부) | `executor_ctl`이 구독 생성(`origin = none`), ENABLE · DISABLE, 슬롯 삭제 모두 됨. `pg_replication_origin_advance`는 `executor_ctl`에 거부. 마스터의 GRANT EXECUTE는 성공을 돌려주지만 효과가 없었음. 마스터는 `pg_subscription`을 못 읽음([G2]와 같음) | `G/rev-sub.json`, `G/privileges.json` |
| SPK-03(G) | 막힘(이름 검증) | CA 파일 불가(`//.postgresql/root.crt does not exist`). **`sslrootcert=system`은 `certificate is not allowed`로 거부.** `require` + `channel_binding=require` 성공. PSA의 Cloud SQL이 고객 VPC의 VM IP(10.60.0.2)로 나가는 길은 동작 | `G/rev-sub.json` |
| SPK-04(G) | 통과(예산 초과) | NOLOGIN 313.6, REVOKE 747.1, 세션 종료 546.9 ms, 합계 1,607.6 ms. 노트북 → IAP → 도쿄 왕복 때문입니다. 엔진 쪽 시간만 따로 재지 않았습니다. 영수증 MATCH | `G/cutover-cloudsql-to-pg.json` |
| SPK-05(G) | 통과(이름 주의) | **서버 인증서 SAN이 `…sql-psa.goog.`처럼 점으로 끝남.** libpq 14 · 16 모두 `host=`에 점까지 써야 verify-full 통과. PgBouncer도 `[databases] host=`와 `/etc/hosts` 항목을 모두 점까지 써야 통과(점 없으면 이름 불일치, hosts만 점 없으면 DNS 조회 실패). PAUSE+RESUME p95 3.4 ms, 클라이언트 오류 0건, 대기 최대 7.3초 | `G/n6-tls-client.txt`, `G/spk05-pgbouncer-names.txt`, `G/spk05-pgbouncer.json` |
| SPK-07 | 통과(문서와 다름) | 공유 CA 모드 + PSA 인증서에 DNS SAN 있음. 이름은 `dnsNames[]`에만 있고(`recordManager: CUSTOMER`), 예전 `dnsName` 필드와 Terraform `dns_name`은 빈 값. **REGIONAL(HA)에서 구독 생성 · ENABLE이 되고, 수동 장애 조치(23초, a → c 영역) 뒤에도 장애 전후 행이 모두 도착함.** 공식 문서의 이유는 "나가는 IP가 일정하지 않음"이었음 | `G/spk07-server-cert.txt`, `G/cloudsql-dns.json`, `G/spk07-ha-subscriber.txt`, `G/spk07-ha-failover.txt` |
| SPK-09(G) | 통과 | 영수증 MATCH. Cloud SQL glibc collation 2.19의 기본 정렬은 RDS(2.26)와 같고 Debian(2.41)과 다름. `COLLATE "C"` 해시는 세 엔진이 같음(`aae5a28f…`) | `G/digest-after-initial-copy.json`, `G/spk09.json` |
| SPK-11 | 막힘(TLS만 시험) | AlloyDB(2 vCPU primary, PSA, `ENCRYPTED_ONLY`) 직접 연결 10.61.7.2:5432. 서버 인증서는 자체 서명 `CN=unused`, SAN `DNS:localhost`. API `generateClientCertificate`의 `caCert`(`CN=root.alloydb`)로 verify-ca · verify-full 모두 `certificate verify failed`. `connectionInfo`에는 인증서 체인이 없음. `require`는 TLS 통과(인증 단계까지 감). 서빙 인증서를 그대로 고정하면 verify-ca와 `host=localhost` verify-full은 통과하지만, 문서에 없는 인증서라 근거로 쓰지 않음. 복제 역할 · 외부 구독은 시험하지 않음 | `G/spk11-alloydb-tls.txt`, `G/alloydb-client-cert-response.json` |

**스키마가 Cloud SQL에서 막히는 곳(측정).** 마스터 `postgres`는 CREATEROLE만 있고 superuser · REPLICATION이 아닙니다.
- `CREATE ROLE … REPLICATION`은 됩니다(RDS와 다름).
- **`CREATE ROLE`을 한 사용자에게 그 역할의 구성원 행이 생기지 않습니다(ADMIN도 없음).** 그래서 `createrole_self_grant`도 효과가 없습니다. `ALTER TABLE … OWNER TO chat_owner`는 되지만 `ALTER FUNCTION … OWNER`는 `must be able to SET ROLE`로 실패합니다. 이어서 GRANT와 `CREATE PUBLICATION`이 소유자 아님으로 모두 실패합니다.
- 대응: `CREATE ROLE chat_owner` 바로 뒤에 `GRANT chat_owner TO CURRENT_USER WITH SET TRUE, INHERIT TRUE`와 `GRANT CREATE ON SCHEMA public TO chat_owner` 두 줄(`deploy/pilot/spike-db/cloudsql_01_schema.sql`). `pg_hba_file_rules` GRANT 네 줄은 뺍니다(`cloudsql_02_roles.sql`). 차이는 `G/schema-variant.diff`입니다.
- `pg_authid`, `pg_shadow`, `pg_hba_file_rules`는 마스터도 읽지 못합니다. 그래서 RDS와 같이 DB-AUTH 입력과 PgBouncer `auth_query`가 막힙니다.

### 11.3 측정으로 새로 나온 것

1. **임시 링크의 `kubectl port-forward`는 연결 하나가 끊겨도 전체가 끝납니다.** RDS 첫 컷오버 시도의 4c(세션 종료)에서, 같은 port-forward를 쓰던 앱 연결이 끊기자 `lost connection to pod`로 port-forward가 끝났습니다. Executor 제어 세션도 같이 끊겨 효과가 UNKNOWN이 됐습니다. 다시 읽으니 4a–4c는 모두 적용돼 있었습니다(epoch 1, NOLOGIN, INSERT 없음, 세션 0). 앱 연결과 제어 연결을 다른 port-forward로 나눈 두 번째 시도는 통과했습니다. 임시 링크에서는 Executor 제어 연결을 전용 port-forward로 둡니다(제안). 증거: `R/cutover-attempt1-link-lost.json`, `R/cutover-attempt1-reread.txt`, `R/link-forward.log`.
2. **Executor `PgBouncerAdmin`은 같은 관리 명령을 여섯 번째에 실패합니다.** psycopg 기본 `prepare_threshold = 5`로 여섯 번째 `PAUSE chat`가 extended protocol로 가고, 관리 콘솔이 `extended query protocol not supported by admin console`로 거부합니다. 5초 heartbeat(`SHOW VERSION`)도 같은 길입니다. 연결에 `prepare_threshold = None`을 주면 20회 모두 통과했습니다. 증거: `R/spk05-admin-autoprepare.txt`. Executor는 다른 갈래가 고치는 중이라 이 스파이크는 고치지 않았습니다.
3. **PAUSE는 서버 연결을 닫고, 그 순간 로그인 중이던 클라이언트를 끊습니다.** RESUME 뒤 첫 쿼리마다 TLS 로그인을 새로 합니다. RDS 경로에서는 `server_login_retry` 동안 클라이언트가 `cached error: pause mode`로 끊겼습니다(679건 중 406건). Cloud SQL 경로에서는 끊김 없이 최대 7.3초 기다렸습니다. 앱 재시도가 필요하고, N11 합격선에 "PAUSE 동안 끊긴 클라이언트 0"을 더해야 합니다(제안). 증거: `R/spk05-pgbouncer.log`, `G/spk05-pgbouncer.json`.
4. **링크 경유 비교와 펜싱은 느립니다.** 10만 행 digest가 RDS 쪽 3.4–8.5초, Cloud SQL 쪽 7.6–21.6초였습니다. 노트북 PG는 0.4초였습니다. Cloud SQL 4단계는 IAP 왕복 때문에 1.6초였습니다. 쓰기 정지 예산을 지키려면 Executor를 출발지 가까이 두거나, 정지 전에 대부분을 비교하고 정지 중에는 barrier 뒤 변경분만 비교해야 합니다(제안).
5. **해시 규칙 v1.1은 세션 `DateStyle`이 ISO여야 합니다.** `DateStyle='SQL, DMY'`에서는 Executor digest가 `can't parse timestamptz`로 멈춥니다. 틀린 해시를 내지 않으므로 안전한 쪽입니다. 비교 세션에서 `DateStyle`을 고정하자는 12절 4번의 근거입니다.
6. **미리 만든 역방향 슬롯은 목적지에서 복제가 아닌 쓰기를 모두 담습니다.** 스파이크 정리 중 superuser가 목적지 표를 TRUNCATE했습니다. `origin = none`이면 이 TRUNCATE가 역방향 ENABLE 때 출발지로 갑니다. 슬롯을 다시 만들어 피했습니다. 목적지 관리 작업을 했다면 역방향 슬롯을 다시 만듭니다(제안). 증거: `R/dst-rev-slot-recreate.txt`.
7. **실패한 문장에 든 비밀번호가 서버 로그에 남습니다.** 기본 `log_min_error_statement = error`라서, 실패한 `CREATE ROLE … PASSWORD`와 `CREATE SUBSCRIPTION … password=`의 문장이 RDS 로그와 노트북 PG 로그에 남았습니다. 스파이크 비밀은 모두 지우고 새로 만들었고, RDS는 지웠습니다. 증거 사본은 가렸습니다(`R/rds-postgresql.log`). Cloud SQL은 Cloud Logging이 인스턴스 삭제 뒤에도 남으므로 `log_min_error_statement = panic`을 먼저 걸고 시험했습니다. 대응: 역할 비밀번호는 SCRAM 검증자로만 보냅니다(현재 `00-bravo-init.sh`가 이렇게 바뀜). 구독 conninfo의 비밀번호는 피할 수 없으므로 관리형에 `log_min_error_statement = panic`을 둡니다(제안, D-26).
8. **마스터 비밀번호는 Terraform 상태에 없습니다.** RDS는 `manage_master_user_password = true`로 비밀을 만들었고, 상태에는 ARN만 남았습니다. 실제 값으로 상태를 grep해 없음을 확인했습니다(`R/state-secret-check.txt`). Cloud SQL `postgres` 비밀번호는 Terraform이 아니라 OpenBao에서 `gcloud … --prompt-for-password`로 파이프 전달했습니다.

### 11.4 다시 돌리는 법과 비용

- IaC: `infra/envs/spike-db`(AWS, 상태 키 `envs/spike-db.tfstate`), `infra/envs/spike-db/gcp`(상태 키 `envs/spike-db-gcp.tfstate`). 모든 자원에 `component=spike`.
- 실행: `deploy/pilot/spike-db/`. `run.sh <facts|privileges|seed|fwd-sub|digest|rev-sub|cutover|failback|pgb|spk09|probe>`, `SPIKE_SIDE=rds|cloudsql`. 비밀은 플랫폼 OpenBao `secret/envs/spike-db/*`에서 환경 변수로만 받습니다.
- 비용(공식 요금표 기준 추정, 실제 청구는 Cost Explorer · Billing에서 `component=spike`로 확인):
  - AWS: RDS 수명 0.625시간. 인스턴스 $0.0156, gp3 $0.0022, 비밀 $0.0003, 전송 상한 $0.02. 합계 상한 약 $0.04(`R/cost.json`).
  - Google: `G/cost.json`.

## 12. 열린 것

| # | 열린 것 | 정할 사람 |
|---|---|---|
| 1 | D-24 채택 여부(D-23의 "관리형 DB를 목적지로 쓰지 않음"과의 관계) | 팀장 |
| 2 | 관리형 구독자의 CA 방식. 측정 결과 Cloud SQL은 채널 바인딩 예외밖에 없음(11.2절). RDS만 공개 CA 인증서 안이 남음 | 보안 통제 · 게이트, 데이터 · 컷오버 |
| 3 | Cloud SQL HA 출발지. 측정으로는 HA 구독자가 동작함(11.2절 SPK-07). HA를 끄라고 요구할지, 출발 IP 허용을 PSA 대역 전체로 두고 진행할지 | 데이터 · 컷오버 |
| 4 | 해시 규칙 v1에 `COLLATE "C"`와 세션 설정을 고정할지. 측정: 기본 collation 정렬이 엔진마다 다름(glibc 2.19 · 2.26 · 2.41), v1.1은 `DateStyle`이 ISO가 아니면 멈춤 | 데이터 · 컷오버(T13) |
| 5 | 관리형 마스터 자격을 둘 곳. 스파이크는 RDS 관리 비밀(Secrets Manager, 상태에 ARN만), Cloud SQL은 플랫폼 OpenBao를 씀 | 인프라 · CI |
| 6 | 관리형용 스키마 변형을 저장소의 정본으로 둘지. RDS는 REPLICATION 대신 `rds_replication`, Cloud SQL은 `chat_owner` 자기 GRANT 두 줄, 둘 다 `pg_hba_file_rules` GRANT 제외(11.1 · 11.2절) | 데이터 · 컷오버 |
| 7 | DB-AUTH 통제 입력을 관리형에서 무엇으로 바꿀지(`pg_hba_file_rules` · `pg_authid` 읽기 불가) | 보안 통제 · 게이트 |
| 8 | Executor `PgBouncerAdmin`의 `prepare_threshold` 수정, 임시 링크에서 제어 연결 분리(11.3절 1 · 2) | 데이터 · 컷오버 |
| 9 | PAUSE 중 클라이언트 끊김을 N11 합격선에 넣을지(11.3절 3) | 데이터 · 컷오버, 앱 |
| 10 | 관리형 끝에서 쓰기 정지 예산을 지킬 비교 방식과 Executor 위치(11.3절 4) | 데이터 · 컷오버 |
| 11 | 아직 돌리지 않은 칸: 관리형끼리(#5, #6, #8, #9), 관리형으로의 정방향 초기 복사, CNPG 구독자(SPK-06), Aurora(SPK-08), 큰 DB 초기 복사(SPK-10) | 데이터 · 컷오버 |

## 13. 출처

모두 2026-11-02에 접속했습니다. Google Cloud 문서는 `cloud.google.com`에서 `docs.cloud.google.com`으로 옮겨진 주소입니다.

| ID | 문서 | URL |
|---|---|---|
| A1 | RDS for PostgreSQL 논리 복제 | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html |
| A2 | Aurora PostgreSQL 논리 복제 설정 | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Replication.Logical.Configure.html |
| A3 | `rds_superuser`, `rds_replication` 역할 | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.rds_superuser.html |
| A4 | 자체 운영 publisher → RDS 구독(DMS 단계별 가이드) | https://docs.aws.amazon.com/dms/latest/sbs/chap-manageddatabases.postgresql-rds-postgresql-full-load-publisher.html |
| A5 | Aurora PostgreSQL 논리 복제 개요 | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Replication.Logical.html |
| A6 | RDS PG17 논리 슬롯 동기화 | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.pglogical.slot.synchronization.html |
| A7 | Aurora 메이저 업그레이드(fast clone + 논리 복제) | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.MajorVersionUpgrade.html |
| A8 | RDS Proxy | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy.html |
| A9 | RDS PostgreSQL SSL, 인증서 | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html · https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html · https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Security.html |
| A10 | VPC 안 DB 접근 경로 | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.Scenarios.html |
| A11 | AWS DMS: PostgreSQL 원본, 검증, 동종 이전 | https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.PostgreSQL.html · https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Validating.html · https://docs.aws.amazon.com/dms/latest/userguide/data-migrations.html |
| A12 | RDS · Aurora 판, 확장 목록 | https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-versions.html · https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Updates.html · https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html |
| A13 | RDS 지표(슬롯 지연, WAL 디스크) | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-metrics.html |
| G1 | Cloud SQL 플래그 | https://docs.cloud.google.com/sql/docs/postgres/flags |
| G2 | Cloud SQL 논리 복제 설정과 제한 | https://docs.cloud.google.com/sql/docs/postgres/replication/configure-logical-replication |
| G3 | Cloud SQL 사용자와 `cloudsqlsuperuser` | https://docs.cloud.google.com/sql/docs/postgres/users |
| G4 | Cloud SQL 문제 해결(`pg_signal_backend`) | https://docs.cloud.google.com/sql/docs/postgres/troubleshooting |
| G5 | Cloud SQL 서버 CA 모드, 사용자 SAN | https://docs.cloud.google.com/sql/docs/postgres/authorize-ssl · https://docs.cloud.google.com/sql/docs/postgres/custom-dns-name |
| G6 | Cloud SQL 사설 IP, 여러 VPC | https://docs.cloud.google.com/sql/docs/postgres/private-ip · https://docs.cloud.google.com/sql/docs/postgres/connect-multiple-vpcs |
| G7 | Cloud SQL 관리형 연결 풀링 | https://docs.cloud.google.com/sql/docs/postgres/managed-connection-pooling |
| G8 | Cloud SQL 판, 확장, DB 만들기 | https://docs.cloud.google.com/sql/docs/postgres/db-versions · https://docs.cloud.google.com/sql/docs/postgres/extensions · https://docs.cloud.google.com/sql/docs/postgres/create-manage-databases |
| G9 | AlloyDB 플래그, 사용자 | https://docs.cloud.google.com/alloydb/docs/reference/alloydb-flags · https://docs.cloud.google.com/alloydb/docs/database-users/overview |
| G10 | AlloyDB 연결 방식, SSL | https://docs.cloud.google.com/alloydb/docs/choose-alloydb-connectivity · https://docs.cloud.google.com/alloydb/docs/instance-ssl |
| G11 | Google Database Migration Service | https://docs.cloud.google.com/database-migration/docs/postgres/configure-source-database · https://docs.cloud.google.com/database-migration/docs/supported-databases · https://docs.cloud.google.com/database-migration/docs/postgres/known-limitations · https://docs.cloud.google.com/database-migration/docs/postgres/promote-migration |
| P1 | PG16 미리 정의된 역할 | https://www.postgresql.org/docs/16/predefined-roles.html |
| P2 | PG16 CREATE SUBSCRIPTION(`origin`, `password_required`) | https://www.postgresql.org/docs/16/sql-createsubscription.html |
| P3 | 논리 복제 제한(PG18), 시퀀스 복제(PG19) | https://www.postgresql.org/docs/18/logical-replication-restrictions.html · https://www.postgresql.org/docs/19/logical-replication-sequences.html |
| P4 | CREATE PUBLICATION, 복제 보안 | https://www.postgresql.org/docs/16/sql-createpublication.html · https://www.postgresql.org/docs/16/logical-replication-security.html |
| P5 | `pg_terminate_backend` | https://www.postgresql.org/docs/17/functions-admin.html |
| P6 | PG16 ALTER ROLE(CREATEROLE과 ADMIN OPTION) | https://www.postgresql.org/docs/16/sql-alterrole.html |
| P7 | 복제 프로토콜 스냅샷, `pg_dump --snapshot`, `pg_createsubscriber` | https://www.postgresql.org/docs/17/protocol-replication.html · https://www.postgresql.org/docs/17/app-pgdump.html · https://www.postgresql.org/docs/17/app-pgcreatesubscriber.html |
| P8 | 메이저 판 사이 논리 복제 | https://www.postgresql.org/docs/17/logical-replication.html |
| P9 | collation 판 변경과 인덱스 | https://www.postgresql.org/docs/17/sql-altercollation.html · https://www.postgresql.org/docs/17/sql-alterdatabase.html |
| P10 | libpq `host`, `hostaddr`, verify-full, `channel_binding` | https://www.postgresql.org/docs/17/libpq-connect.html · https://www.postgresql.org/docs/17/libpq-ssl.html |
| P11 | PG17 논리 복제 failover 슬롯 | https://www.postgresql.org/docs/17/logical-replication-failover.html |
| B1 | PgBouncer 명령과 설정 | https://www.pgbouncer.org/usage.html · https://www.pgbouncer.org/config.html |
| N1 | CNPG 1.30 논리 복제, 지원 판 | https://cloudnative-pg.io/docs/1.30/logical_replication · https://cloudnative-pg.io/docs/1.30/supported_releases |
