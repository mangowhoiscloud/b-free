# 스펙 추적표 (v0.1)

스펙의 요구, 시나리오 인수 조건, 작업을 저장소 경로와 시험에 잇습니다. 스펙 문장은 요약만 하되, 완료 판정에 쓰는 조건은 빼지 않습니다. 원문은 스펙을 봅니다. 작업 표의 상태는 "계획", "진행", "완료" 중 하나이고, 끝나면 PR에서 고칩니다.

담당 영역 이름은 스펙 §11.2를 따릅니다. 담당자 이름은 11/2 21:00 회의에서 채웁니다.

## 1. 기능 요구 (§5.1)

| ID | 요약 | 구현 위치 | 작업 | 우선 |
|---|---|---|---|---|
| FR-1 | 출발지와 목적지 설정을 읽기 전용으로 수집 | `platform/collector/` | 전용 작업 없음(아래 공백 1) | P0 |
| FR-2 | 의존성 정적 탐지와 3등급(설정 교체, 어댑터 추가, 코드 수정) 분류. 애매한 것만 AI | `platform/agent/` (분석 Trial) | T16 | P0 |
| FR-3 | 목적지를 Terraform과 공통 설치로 같은 기준으로 구축. 클러스터는 K3s(D-21), 묶음은 랜딩 존(D-23), 객체 저장은 제안 D-22(검증 중) | `infra/`, `deploy/landing-zone/` | T2, T9 | P0 |
| FR-4 | AI는 스키마 제안서로만 번역안과 수정안을 냄 | `platform/agent/` | T11 | P0 |
| FR-5 | 제안서는 리허설 네임스페이스에서만 시험. 별도 검증 Job, 최대 3회 | `platform/rehearsal/` | T3, T15 | P0 |
| FR-6 | 원본 대조 게이트. 하나라도 다르거나 UNKNOWN이면 차단 | `platform/gate/` | T19 | P0 |
| FR-7 | 게이트 통과와 승인 없이는 컷오버 불가. 승인은 값에 결속 | `platform/api/` (상태 머신) | T10, T15 | P0 |
| FR-8 | Edge 가중치 10, 50, 100%. 기준 초과 시 0 | `platform/api/edge/`, `infra/modules/edge/` | T17 | P0 |
| FR-9 | 논리 복제, PAUSE와 RESUME, 펜싱, barrier. 쓰기 DB는 최대 하나 | `platform/executor/` | T12 | P0 |
| FR-10 | 컷오버와 되돌리기마다 쓰기를 막고 행 수, PK 집합 해시, 내용 해시, 시퀀스를 비교해 영수증을 남김. 다르면 취소 | `platform/executor/integrity/` | T12, T13, T20 | P0 |
| FR-11 | 역방향 복제로 데이터째 되돌리기 | `platform/executor/` | T20 | P0 |
| FR-12 | 6개 통제를 전후에 같은 방법으로 시험, 통제 ID별 비교표 | `platform/gate/controls/` | T5, T14 | P0 |
| FR-13 | 콘솔 한 화면: 진행, 판정, 비교표, 쓰기 공백, 의존성 지도 | `platform/console/` | T21 | P0 |
| FR-14 | 수정 불가 저장소에 모든 기록 | `platform/api/audit/` | T24 | P1 |
| FR-15 | Exit Drill: 출발지 접근을 끊고 재시작 · 새 연결까지 동작하는지 시험. 복제 채널은 유지 | `platform/executor/drill/` | T27 | P1 |

## 2. 비기능 요구 (§5.2)

| ID | 요약 | 확인 위치 | 작업 |
|---|---|---|---|
| NFR-1 | 쓰기 정지 5초 이하(목표). 부하 상태 5회의 원측정값, 중앙값, 최댓값 공개 | `tools/loadgen/`, `platform/executor/` | T12, T18, T25 |
| NFR-2 | 실패 · 유실 · 중복 0건. 요청 ID와 시도 ID 대조 | `tools/loadgen/` | T18 |
| NFR-3 | 불명확하면 SAFE_HOLD. 시간 초과만으로 재개하지 않음 | `platform/api/`, `platform/executor/` | T12, T25 |
| NFR-4 | 에이전트는 자격 없이 도구 계층으로만. 송신 기본 거부 | `platform/agent/`, `platform/rehearsal/`, `policies/` | T3, T11 |
| NFR-5 | 게이트 · 컷오버 · 롤백 모듈은 LLM을 부르지 않음. 같은 입력이면 같은 판정 | import 검사 스크립트(계획), 게이트 반복 시험 | T12, T19, T20 |
| NFR-6 | 장기 키 없음(OIDC, IAM 역할). 비밀 평문 금지 | `infra/modules/ci_identity/`, `infra/modules/secret/` | T1, T7, T9 |
| NFR-7 | 모든 환경을 IaC로 생성 · 삭제 | `infra/envs/` | T1, T2, T6, T7 |
| NFR-8 | `component` 태그, 예산 알림, 비용 분리 | `infra/modules/*` | T1 |

## 3. 시나리오 인수 조건 (§6)

| ID | 요약 | 시험 경로(계획) | 작업 |
|---|---|---|---|
| S1-AC1 | 빈 VM에서 목적지 클러스터가 CIS 기준 설정으로 뜨고 기록이 남음. D-21에서는 K3s와 CIS 하드닝 가이드 설정 | `tests/e2e/s1_build` | T2 |
| S1-AC2 | 6개 통제가 통제 ID별 PASS, FAIL, UNKNOWN으로 나옴 | `tests/e2e/s1_controls` | T14 |
| S1-AC3 | 예외 승인 없는 FAIL이나 UNKNOWN이면 이전 후보 등록이 막힘 | `tests/e2e/s1_gate_block` | T19, T10 |
| S2-AC1 | 리허설의 운영 · 외부 접속이 막히고 기록됨 | `platform/rehearsal/` 격리 시험 | T3 |
| S2-AC2 | 승인이 없으면 목적지에 반영되지 않음 | `tests/e2e/s2_no_approval` | T15, T10 |
| S2-AC3 | 배포 digest가 승인 값과 다르면 거부 | `tests/e2e/s2_digest_mismatch` | T15 |
| S2-AC4 | 끝난 리허설의 네임스페이스와 비밀이 남지 않음 | `platform/rehearsal/` 잔존 검사 | T3, T15 |
| S2-AC5 | 컷오버가 확정된 목적지에서는 리허설 시작을 거부 | `tests/e2e/s2_after_cutover` | T10 |
| S3-AC1 | 쓰기 50 req/s에서 실패 · 유실 · 중복과 쓰기 정지 기록. 목표 0건, 5초 | `tests/e2e/s3_cutover_load` | T12, T18 |
| S3-AC2 | 양쪽 쓰기를 막고 행 수, PK 해시, 내용 해시, 시퀀스를 비교. 결과를 목적지 쓰기 전에 저장 | `tests/e2e/s3_compare_first` | T12, T13 |
| S3-AC3 | 이전 뒤 통제 재시험에서 예외 승인 외 FAIL과 UNKNOWN 0개 | `tests/e2e/s3_controls_after` | T14 |
| S3-AC4 | 심사위원 채팅 기록이 남고 새 메시지 ID가 충돌하지 않음 | `tests/e2e/s3_chat_continuity` | T12, T22 |
| S4-AC1 | 되돌린 뒤 전환 이후 메시지가 출발지에 모두 있음 | `tests/e2e/s4_failback` | T20 |
| S4-AC2 | 승인자, 시각, 복구 시간, 영수증이 남음 | `tests/e2e/s4_records` | T20, T24 |

## 4. 작업 (§11.1)

| 작업 | 이름 | 영역 | 경로 | 완료 기준(요약) | 선행 | 상태 |
|---|---|---|---|---|---|---|
| T1 | AWS 기반: 권한, VPC 2개, 비용 태그 | 인프라 · CI | `infra/modules/network/aws`, `infra/envs/` | VPC 2개 생성 · 삭제 1회, 모든 자원에 태그 | 없음 | 계획 |
| T2 | 온프렘 목적지: K3s(CIS 하드닝) + 노드 준비. 작업 이름은 D-21이 바꿈 | 인프라 · CI | `infra/modules/cluster/k3s`, `infra/envs/target-onprem/` | Lima VM 2대(server 1, agent 1)에서 K3s가 하드닝 설정으로 뜨고 kube-bench(K3s 벤치마크) 결과. StorageClass `bravo-db`, `bravo-general`이 있음(저장소는 D-22 검증 결과를 따름) | 없음 | 계획 |
| T3 | 리허설 네임스페이스 격리 실측 | 에이전트 · 리허설 | `platform/rehearsal/`, `policies/rehearsal/` | 격리 시험이 통과하고 생성 직후 자동 실행 | 없음 | 계획 |
| T4 | 데모 앱 v0: 채팅 스텁 | 데모 앱 · 통합 | `apps/demo/` | 두 브라우저 메시지와 첨부, 멱등 키 중복 없음 | 없음 | 계획 |
| T5 | 6개 통제 시험 설계 | 보안 통제 · 게이트 | `platform/gate/controls/` | EKS와 K3s에서 같은 뜻으로 비교할 통제 × 시험 × 합격 기준 표 확정, 리뷰 승인. kube-bench는 벤치마크가 달라 같은 뜻의 항목만 비교(`docs/architecture/04-governance.md` 1절) | 없음 | 계획 |
| T6 | 출발지: EKS + CNPG + PgBouncer + ESO | 인프라 · CI | `infra/envs/source-aws/` | 데모 앱 v0이 EKS에서 PgBouncer 경유로 동작 | T1 | 계획 |
| T7 | 플랫폼 클러스터(EKS) + Actions OIDC. 작업 이름은 D-21이 바꿈 | 인프라 · CI | `infra/modules/cluster/eks`, `infra/envs/platform-aws/`, `.github/workflows/` | OIDC로 플랫폼 배포 1회. 관리형 노드 그룹 core가 3 AZ에 하나씩, agents에 taint. core 노드 하나를 지워도 API와 상태 머신이 응답 | T1 | 계획 |
| T8 | 복제 경로: Tailscale + 논리 복제 왕복(스펙 이름 그대로. 제안 D-16은 연결을 Cloudflare 링크로 바꿈) | 데이터 · 컷오버 | `infra/`, `platform/executor/` | 양방향 도착과 지연값 기록 | T2, T6 | 계획 |
| T9 | 목적지 보안 구성요소: OpenBao, ESO, S3 호환 저장소(후보 Rook-Ceph RGW, 제안 D-22, 검증 중) | 보안 통제 · 게이트 | `infra/modules/secret/onprem`, `infra/modules/object_store/onprem` | 앱 전용 자격만 버킷 접근, 무인증 거부 | T2 | 계획 |
| T10 | 상태 머신 + 플랫폼 API + 이벤트 스트림 | 데이터 · 컷오버 | `platform/api/` | 목업으로 COLLECT에서 DONE까지, 조건 미충족 전이 거부 | T7 | 계획 |
| T11 | 에이전트 워커 + 도구 계층 + 제안서 스키마 | 에이전트 · 리허설 | `platform/agent/` | 틀린 스키마와 차단 명령이 거부 · 기록 | T7 | 계획 |
| T12 | Cutover Executor | 데이터 · 컷오버 | `platform/executor/` | 컷오버 1회, ABORT_PRE_WRITE와 SAFE_HOLD 각 1회 | T8, T10 | 계획 |
| T13 | 무결성 비교와 영수증 | 데이터 · 컷오버 | `platform/executor/integrity/` | 한 행 어긋나면 MISMATCH, 맞추면 MATCH. 영수증 저장 | T8 | 계획 |
| T14 | 통제 시험기와 비교표 | 보안 통제 · 게이트 | `platform/gate/controls/` | 6개 통제가 양쪽에서 결과를 냄 | T5, T6, T9 | 계획 |
| T15 | 리허설 러너: 작업 1건, 검증 Job, 승인 결속 | 에이전트 · 리허설 | `platform/rehearsal/` | 리허설, 검증 통과, 승인 대기(diff와 digest 결속) | T3, T11 | 계획 |
| T16 | 의존성 인벤토리: 정적 탐지 + 3등급 | 에이전트 · 리허설 | `platform/agent/inventory/` | DB 주소, S3, Secrets Manager, KMS가 등급과 함께 나옴 | T4, T11 | 계획 |
| T17 | Edge: Cloudflare LB + Tunnel 가중치 전환 | 콘솔 · 관측 · Edge | `infra/modules/edge/`, `platform/api/edge/` | 10, 50, 100, 0% 전환과 반영 시간 기록 | T2, T6 | 계획 |
| T18 | 부하 생성기와 관측 | 콘솔 · 관측 · Edge | `tools/loadgen/` | 실패 · 유실 · 중복과 쓰기 공백 자동 집계 | T4 | 계획 |
| T19 | 원본 대조 게이트 | 보안 통제 · 게이트 | `platform/gate/` | 열린 버킷과 스키마 불일치 BLOCK, 고친 뒤 PASS. 같은 입력이면 판정이 항상 같음 | T13, T14 | 계획 |
| T20 | 되돌리기(failback) | 데이터 · 컷오버 | `platform/executor/` | 전환 뒤 메시지가 되돌린 뒤 출발지에 모두 있음 | T12, T13 | 계획 |
| T21 | 콘솔 | 콘솔 · 관측 · Edge | `platform/console/` | 10장 데모 흐름 전체를 한 화면에서 조작 | T10, T18 | 계획 |
| T22 | 데모 앱 v1 | 데모 앱 · 통합 | `apps/demo/` | 컷오버 중 휴대폰 채팅이 이어지고 환경 표시가 바뀜 | T4 | 계획 |
| T23 | 규제 매핑표 원문 확인 (P1) | 보안 통제 · 게이트 | `platform/governance/packs/` | 매핑표의 "원문 확인" 칸이 출처와 함께 채워짐 | T5 | 계획 |
| T24 | 감사 로그(S3 Object Lock) (P1) | 보안 통제 · 게이트 | `platform/api/audit/` | 데모 1회 기록 조회, 삭제 시도 거부 | T10 | 계획 |
| T25 | 반복 측정 · 장애 주입 · SAFE_HOLD 시험 (P1) | 데이터 · 컷오버 | `tests/e2e/`, `tools/loadgen/` | 측정 리포트와 장애별 결과 | T12, T18, T20 | 계획 |
| T26 | Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 평가: 결함 시나리오 반복 (P1) | 에이전트 · 리허설 | `platform/rehearsal/` | 발표용 수치 표 1개 | T15 | 계획 |
| T27 | Exit Drill (P1) | 데이터 · 컷오버 | `platform/executor/drill/` | 차단 상태에서 목적지 앱 동작, 남은 의존성 지도 | T12 | 계획 |
| T28 | 통합 리허설 · 데모 각본 · reset | 전원 | `tests/e2e/`, `docs/runbooks/` | 270초 안에 끝나는 리허설 3회 연속 | T15, T16, T17, T19, T20, T21, T22 | 계획 |
| T29 | 결정 문서 · ADR · 설계 문서 갱신 (P1) | 전원 | `docs/adr/` | 결정 문서의 모든 결정에 ADR | 없음 | 계획 |

가장 긴 경로는 T1, T6, T8, T12, T20, T28입니다(§11.1).

**제안 D-16이 채택되면 더하는 완료 기준.** 시험 ID는 `docs/architecture/02-network.md` 13절입니다.

| 작업 | 더하는 것 |
|---|---|
| T1 | 피어링 대신 사설 서브넷과 Regional NAT. `edge_link` 모듈(주인 인프라 · CI)과 토큰 부트스트랩 스크립트. N1, N2, N3, N14 통과 |
| T6 | VPC 안 CodeBuild로 `cf-edge`와 Argo CD 첫 설치. 끝난 뒤 CodeBuild의 클러스터 권한과 SG 규칙 제거 |
| T7 | 플랫폼 클러스터도 CodeBuild 첫 설치 뒤 Argo CD pull. N2, N4(11/6 회전 연습), N7, N8, N17, N18 통과 |
| T8 | 링크 위 복제. N5, N5b, N5c, N6, N10, N11, N12, N16 결과 기록. 11/4 18:00 판정은 11/3 밤 축소 N5와 N11로 함(D-16 "결정 시점과 선택 규칙") |
| T10, T11 | 테넌트 격리. `auth_lookup`, N9, N15, N18 통과 |
| T17 | `edge_link`의 Access 정책 내용 검토(N3 결과 확인). 모듈 주인은 T1 |
| T25 | N11 반복 측정, N13, N13b |

## 5. 스펙에서 찾은 공백

스펙을 저장소 경로로 옮기다 보니 빈 곳이 셋 보였습니다. 고치는 방법은 `DEVIATIONS.md`에 제안으로 적었습니다.

1. **FR-1 수집기를 맡은 작업이 없습니다.** T14(통제 시험기), T16(인벤토리), 상태 머신의 COLLECT가 수집 결과를 씁니다. 그런데 수집기를 만드는 작업이 목록에 없습니다(D-05).
2. **첨부 파일 이전이 컷오버 절차에 없습니다.** §7.3은 첨부 저장소를 "엔드포인트 설정 교체"로 둡니다. 주소만 바꾸면 S3의 기존 파일은 목적지 저장소로 옮겨지지 않습니다. §8.6의 비교와 복제도 DB만 다룹니다(D-04).
3. **P0 AI 작업의 범위가 절마다 다릅니다.** §8.4는 P0 작업을 "1건"(Secrets Manager 코드 수정)으로 둡니다. S2 기본 흐름 3과 §10 데모는 목적지 버킷 정책 작성과 열린 첨부 저장소 BLOCK의 해소까지 보입니다. 대상 저장소는 제안 D-22에서 고릅니다. 이 저장소는 S2와 §10을 따라 버킷 정책 번역을 P0에 넣습니다(`docs/architecture/05-agent-rehearsal.md` 1절).

스펙 공백은 아니지만 구현에서 고친 것도 있습니다. 프로토타입의 문자열 차단 목록은 변형 명령으로 우회됐습니다. 스펙은 차단 목록의 구현 방식을 정하지 않았으므로, 구조화한 작업으로 바꿨습니다(D-06).
