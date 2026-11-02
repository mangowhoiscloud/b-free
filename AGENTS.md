# Bravo 플랫폼: 작업 규칙

사람과 코딩 에이전트가 같이 따르는 규칙입니다. 제품 설계는 스펙 v0.1이 정본입니다: https://app.notion.com/p/40b8bee9ada480ecaca6f716e123368b
이 파일은 코드만 봐서는 모르는 사실과 규칙만 둡니다.

## 스펙 우선

- 스펙과 이 저장소 문서가 다르면 스펙이 이깁니다.
- 코드, 문서, 이슈, 커밋, PR에 스펙 ID를 붙입니다. 요구는 `FR-n`, `NFR-n`, 작업은 `T-n`, 시나리오는 `S1`–`S4`, 절은 `§8.6`입니다.
- 커밋과 PR 제목은 `[T12] 펜싱 시험 추가`처럼 작업 번호로 시작합니다(스펙 §12).
- 스펙에 없는 일은 `docs/spec/DEVIATIONS.md`에 "제안"으로 먼저 적습니다. 회의에서 정하기 전에는 구현하지 않습니다.

## 어디를 읽을까

| 이 작업이면 | 먼저 읽을 것 |
|---|---|
| 요구가 어느 폴더로 가는지 | `docs/spec/SPEC-TRACE.md` |
| 스펙과 다른 제안 | `docs/spec/DEVIATIONS.md` |
| 1급 스펙 원천(스펙과 함께 따르는 팀 문서, 사본, 갱신 절차) | `docs/spec/SOURCES.md` |
| 전체 구조 | `docs/architecture/00-overview.md` |
| 환경 넷과 크기 | `docs/architecture/01-environments.md` |
| 네트워크, 포트, DB 역할 | `docs/architecture/02-network.md` |
| 상태 머신, 컷오버, 되돌리기, 이전 완수 기준 | `docs/architecture/03-migration.md` |
| 6개 통제, 게이트, 거버넌스 팩 | `docs/architecture/04-governance.md` |
| 에이전트, 도구 계층, 리허설 | `docs/architecture/05-agent-rehearsal.md` |
| 기술 스택 정본(클러스터 종류, 저장소, 판, 열린 결정) | `docs/spec/TECH-STACK-SPEC.md` |
| 언어, 라이브러리, 버전 | `docs/architecture/06-tech-stack.md` |
| Pod 격리, Istio, OTel, 에이전트 상태 주입 | `docs/architecture/07-runtime-mesh-observability.md` |
| 운영자 MCP 서버(`operator-mcp`), 운영자 인증, 승인과 실행 시작이 MCP에 없는 이유 | `docs/architecture/08-operator-mcp.md` |
| CNI, 네트워크 정책 집행, Kyverno 기본선, CSI, StorageClass(`bravo-db`, `bravo-general`), MTU | `docs/architecture/09-cni-csi.md` |
| 클러스터 넷의 구성과 연결 한 장 | `docs/architecture/10-cluster-overview.md` |
| 모든 고객 클러스터에 같은 랜딩 존, 환경 만들기 흐름(D-23) | `docs/architecture/11-landing-zone.md` |
| 에이전트 역할 여섯, 상태별 배치, 책임 경계, 허용 목록, 샌드박스 이미지, SDK 설정(D-27) | `docs/architecture/13-agents.md` |
| Executor 효과 원장, 자원 잠금, 펜싱, 재시작 | `platform/executor/README.md` |
| 승인 결정, 실행 시작 트랜잭션, 공통 인가 | `platform/api/README.md` |
| 일정과 의존 | `docs/plans/2026-11-03-to-07.md` |
| DB 엔진 사이 이전 3×3(RDS · Cloud SQL · CNPG) | `docs/architecture/12-db-migration-matrix.md` |
| IaC로 환경이 펼쳐지는 과정(플랫폼 첫 기동 · 목적지 만들기 · 앱 첫 배포) | `docs/architecture/14-provisioning-scenarios.md` |
| 환경 사이 트래픽 전환(Edge 가중치, 판정, 0% 복귀, LB 구독 전후) | `docs/architecture/15-multicloud-traffic.md` |
| Terraform 모듈 | `infra/README.md` |
| 플랫폼 서비스 경계 | `platform/README.md` |
| 리허설 프로토타입 코드 | `platform/rehearsal/AGENTS.md` |
| 정한 결정 | `docs/adr/README.md` |

## 코드만 봐서는 모르는 것

- 게이트, Executor, 상태 머신 전이 코드는 LLM 클라이언트를 import하지 않습니다(NFR-5). 판정과 실행은 규칙이 합니다.
- 에이전트는 클러스터 자격증명이 없습니다. 도구 계층이 리허설 네임스페이스 한정 서비스 계정으로 대신 실행합니다(NFR-4, §8.4).
- 모든 실행 요청에는 `Idempotency-Key`가 있습니다. 같은 키에 다른 입력이 오면 거부합니다(§8.9). 키의 범위는 (테넌트, 동작, 입력 digest)이고, 같은 요청은 같은 실행 handle을 받습니다.
- Executor는 낮은 epoch의 명령을 거부합니다. 응답을 잃으면 같은 호출을 반복하지 않고 실제 상태를 다시 읽습니다(§8.6).
- 상태가 불명확하면 SAFE_HOLD입니다. 시간 초과만으로 쓰기를 다시 열지 않습니다(NFR-3).
- 사람 승인과 보안 보류 해제는 콘솔에서만 합니다. MCP 도구로 만들지 않습니다(D-18, 제안 D-20). 승인자는 콘솔 세션의 주체이고, 요청 본문에서 받지 않습니다.
- 트래픽 전환, 컷오버, 되돌리기는 승인 뒤 사람 운영자가 콘솔에서 시작합니다. MCP 도구로 만들지 않습니다. 스펙 §8.2가 이 구간을 AI가 관여하지 않는 실행 구간으로 두기 때문입니다(D-18).
- 모든 처리기는 먼저 `authorize(principal, action, channel)`를 부릅니다. 주체 키는 이메일이 아니라 Access JWT의 `(iss, sub)`입니다.
- (제안 D-19) Executor는 바깥 변경을 효과 원장에 먼저 적고 부릅니다. P0에는 자동 인계가 없습니다. 미해결 효과가 남은 채 다시 뜨면 읽기 전용 자격의 관찰 전용 모드로만 뜨고, 계획은 SAFE_HOLD입니다. DB 명령은 양쪽 DB의 `bravo_fence`가 옛 epoch를 거부합니다. Executor는 StatefulSet replica 1이고, 강제 삭제하지 않습니다.
- 출발지와 목적지는 GHCR의 같은 digest를 받습니다. 태그로 배포하지 않습니다(§8.8).
- 데모 앱 유형은 스펙의 잠정안(사람 간 실시간 채팅)이고, 기반 오픈소스 선택은 비워 둡니다. 플랫폼이 앱에 요구하는 조건은 `apps/demo/README.md`에 있습니다.
- CUTOVER부터 DONE까지는 앱 배포가 잠깁니다. PgBouncer의 대상 DB는 Executor만 바꿉니다.

## 멈추고 사람에게 묻는 일

- 클라우드 자원 생성과 삭제(`terraform apply`, `terraform destroy`), 비용이 드는 모든 작업
- 공유 브랜치 force-push, 저장소 설정 변경, 외부 메시지 전송
- 실제 자격증명이 필요해 보이는 작업. 장기 키를 만들지 않습니다(NFR-6). 목적지 비밀은 OpenBao와 ESO, CI는 GitHub OIDC, 플랫폼 비밀은 `platform/README.md`의 표를 따릅니다. 출발지 앱의 Secrets Manager 직접 호출은 S2의 재료로 일부러 둔 의존성입니다(§8.10).
- 게이트 판정 규칙, 통제 시험의 합격 기준, 리허설 검증기 수정. 판정 기준이 바뀌기 때문입니다.
- `docs/spec/spec-*-snapshot.html` 수정. 사본은 고치지 않고 새 판을 받습니다.

## 완료 조건

- `make check`가 통과합니다. 바꾼 영역의 시험도 통과합니다.
- PR 본문에 작업 번호, 실행한 명령, 증거 경로(`docs/evidence/` 행, 실행 ID)를 적습니다.
- 작업의 완료 기준은 `docs/spec/SPEC-TRACE.md` 4절의 표를 따릅니다. 끝나면 상태 칸을 고칩니다.
- 구조 결정이면 ADR을 추가하고 위 표를 고칩니다.

## 규칙과 강제 수단

| 규칙 | 강제 수단 | 상태 |
|---|---|---|
| AGENTS.md 색인 경로, 문서 안 상대 링크, ADR 색인이 실제 파일과 일치 | `scripts/check_docs.py` | 동작. 스펙 ID, 우선순위, 편차 상태는 검사하지 않음 |
| 게이트 · Executor · 상태 머신은 LLM을 import하지 않음 | import 검사 스크립트(만들 예정) | 계획(T12, T19) |
| 커밋과 PR 제목에 작업 번호 | CI 제목 검사 | 계획 |
| Git에 평문 비밀 없음 | gitleaks (CI) | 계획(T14) |
| 도구 계층은 구조화한 작업만 받고 파괴적 동사를 거부 | `platform/rehearsal/tests/test_tool_layer.py` | 동작(프로토타입) |
| 리허설 검증기는 산출물을 import하지 않고 보상 조작을 막음 | `platform/rehearsal/tests/test_verifier.py` | 동작(프로토타입) |
| 모든 자원에 `component` 태그 | Terraform `default_tags` | 계획(T1) |
| 렌더링한 매니페스트: taint · toleration 쌍, 제어 구성요소 core 고정, digest 고정, 에이전트 토큰 마운트 끔, DB AZ 분산 | 렌더링 시험(CI) | 계획(T7, T11) |
