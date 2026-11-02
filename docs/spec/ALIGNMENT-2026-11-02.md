# 1급 원천 정렬표 (2026-11-02)

1급 원천(`SOURCES.md`)을 저장소의 현재 결정과 행마다 맞춰 봅니다. 기준 시각은 2026-11-02 19:35 KST입니다. 대조한 저장소 문서는 `TECH-STACK-SPEC.md`, `DEVIATIONS.md`, `docs/architecture/02-network.md`, `07-runtime-mesh-observability.md`, `09-cni-csi.md`, `11-landing-zone.md`입니다. `13-agents.md`는 아직 없습니다.

팀장이 오늘 「기술 스택(툴) 조합」을 읽고 정한 것은 "해결(팀장 결정 11/2)"로 적었습니다. 이 결정은 코디네이터가 19:3x KST에 전달했습니다. 나머지 차이는 "열림"입니다. 이 문서는 정렬 결과만 적습니다. 다른 아키텍처 문서는 고치지 않았습니다(2절에 고칠 곳을 모았습니다).

**칸의 뜻.** 판정은 일치, 차이, 빈 곳(원천에는 있고 저장소에는 없음) 셋입니다. 결정 필요 여부는 해결, 열림(팀장 결정 필요), 문서 정리, 아니오 넷입니다.

## 1. 「기술 스택(툴) 조합」과 저장소

원천: [sources/tech-stack-combination.md](sources/tech-stack-combination.md)

| # | 책임 | 원천의 선택 | 현재 저장소 결정 | 일치/차이/빈 곳 | 결정 필요 여부 |
|---|---|---|---|---|---|
| 1 | 실행 조정 | PostgreSQL 원장 · outbox · 리스 워커. 책임: 승인 · 실행권 · 효과 기록 · 불확실한 결과의 복구 | 효과 원장과 자원 잠금(제안 D-19), epoch 펜싱, `Idempotency-Key`, 불명확하면 SAFE_HOLD. 상태 머신은 Lease로 활성 1, Executor는 StatefulSet replica 1(`10-cluster-overview.md`). 첨부는 첨부 행 테이블이 outbox를 겸함(D-04) | 대체로 일치. 차이: 원천의 "리스 워커"는 여러 워커가 리스로 실행권을 나누는 모양. 저장소 Executor는 replica 1 + epoch이고, lease 만료만으로 잠금이 풀리지 않음. 범용 outbox는 없음 | 열림: D-19 채택(21:00 회의). 리스 워커 모양을 따를지는 Executor 담당이 확인 |
| 2 | 앱 실행 | Kubernetes, 온프렘은 k3s부터 / 기존 ECS · VM 어댑터 | 플랫폼 · 출발지 EKS, 온프렘 K3s, GCP GKE(D-21). ECS · VM 어댑터 없음 | K3s는 일치. 빈 곳: ECS · VM 출발지 | 해결(팀장 결정 11/2): ECS · VM 어댑터는 P2. 기록은 2절 |
| 3 | 트래픽 전환 | Envoy 기반 안정 진입점, 환경별 ALB · Gateway API 연결. 책임: 단계적 전환 · 실제 경로 확인 · 복귀 | 스펙 FR-8: Cloudflare LB 가중치 10 → 50 → 100%, 기준을 넘으면 0. `TECH-STACK-SPEC.md` 1절: 환경 사이는 Cloudflare LB, 환경 안 진입점은 Gateway API(Envoy, Cilium 구현), cloudflared는 Gateway로 보냄. 실제 경로 확인은 D-10(LIVE · RECORDED). 인바운드 0 원칙이라 ALB 없음(D-16) | 해결된 부분: Cloudflare 전역 가중치 + 환경별 Gateway API(Envoy). 남은 차이: (a) 원천에서 안정 진입점은 Envoy, 저장소에서는 Cloudflare. 스펙은 Cloudflare를 잔존 의존성으로 두고 독립 대체 진입점을 비목표로 둠(§4, §8.7) (b) 환경별 ALB를 쓰지 않음 | 해결(팀장 결정 11/2). (b)는 "ALB 대신 Tunnel → Gateway"로 읽어도 되는지 확인만 남음 |
| 4 | 데이터 이전 | PostgreSQL 논리 복제 + 목적지 CloudNativePG. 책임: writer 통제 · 최종 동기화 · 무결성 · 신규 쓰기 이후 복구 | 스펙 §8.6 그대로: 논리 복제, PgBouncer PAUSE, `bravo_fence`, 무결성 영수증, 역방향 복제로 되돌리기. 출발지도 CNPG(EKS) | 일치. 원천은 출발지 DB를 정하지 않음(RDS도 가능). 저장소는 출발지 RDS 지원이 열림(`TECH-STACK-SPEC.md` 3절 2번. 스펙 §4는 P2) | 열림(기존 안건 2번 그대로) |
| 5 | 연결 안정화 | PgBouncer. 책임: 대기 한도 · 타임아웃 · 전환 실패 처리 | PgBouncer 1.26 직접 빌드. `query_wait_timeout`. PAUSE 무응답은 효과 `UNKNOWN`, 원장이 미적용을 증명하지 못하면 SAFE_HOLD(`02-network.md` 4.4절, N13b) | 일치 | 아니오 |
| 6 | 보안 관측 · 격리 | 공급자 보안 이벤트 연결 + Cilium/Hubble, 필요 시 Tetragon. 책임: 공통 통제 항목으로 정규화 · 영향 판단 · 승인된 격리 | `TECH-STACK-SPEC.md`와 `11-landing-zone.md`: 모든 환경 Cilium + Hubble, Hubble 흐름이 NET-01 증거, Tetragon P1. 승인된 격리는 D-20 보류의 "허용 대응". 공급자 보안 이벤트(GuardDuty, CloudTrail 탐지, GCP Security Command Center)를 받는 경로는 없음. AUD-01은 감사 로그 검색만 함 | 해결된 부분: Cilium + Hubble, Tetragon P1. 빈 곳: 공급자 보안 이벤트를 통제 ID로 정규화하는 경로. 문서 안 모순: `09-cni-csi.md`는 아직 환경별 기본 CNI(VPC CNI, flannel)이고 4절에서 "Cilium 직접 운영"을 뺌 | 해결(팀장 결정 11/2) + 열림: 공급자 보안 이벤트 연결의 우선순위(P0 · P1 · P2) |
| 6a | (메시와의 관계) | 원천은 서비스 메시를 적지 않음 | 플랫폼 EKS에 Istio ambient L4(제안 D-15, 11/4 판정). `TECH-STACK-SPEC.md` 2.2의 Istio 행은 대안 칸에 "Cilium: CNI가 관리형마다 달라 뺌"이라는 옛 이유를 적음 | 차이(저장소 안 모순). Cilium이 모든 환경에 들어오면서 D-15의 대안 비교가 낡음 | 열림: D-15를 유지할지(Cilium 위 ambient), Cilium 기능으로 대신할지. 11/4 판정 전에 대안 칸을 고침 |
| 7 | 정책 · 공급망 | OPA + Kyverno + cosign · 취약점 검사. 책임: 정책 예외 · 검증 의무 · 승인 산출물과 실행 결과 연결 | Kyverno 1.19.2(기본 차단 생성, digest 강제). OPA는 계획 · IaC 정책 검사. cosign · SBOM · Trivy는 제안. GHCR digest 고정(§8.8). 예외 승인은 승인 API의 단계(D-11). 스펙은 Kyverno를 P1로 둠 | 해결된 부분: OPA를 Kyverno와 함께 씀. 빈 곳: OPA가 도는 자리(CI, 게이트, admission)와 판이 없음. "승인 산출물과 실행 결과 연결"은 digest와 영수증으로 일부만 있음 | 해결(팀장 결정 11/2) + 열림(작음): OPA 실행 자리와 판, cosign 서명 검증을 Kyverno로 강제할지 |
| 8 | 신원 · 비밀 | 조직 OIDC, 환경별 workload identity / Secrets Manager · OpenBao 연결. 책임: 권한 매핑 · 단기 위임 · 교차 앱 접근 거부 확인 | 운영자는 Cloudflare Access JWT `(iss, sub)`, CI는 GitHub OIDC, EKS Pod Identity · GKE Workload Identity(`11-landing-zone.md` 3절). 목적지 비밀은 OpenBao + ESO. D-23 본문과 `TECH-STACK-SPEC.md` 1절은 "Secrets Manager는 첫 이전 때 번역할 출발지로만"이라고 적음. 교차 앱 거부는 SEC-01 하위 시험 | 일치: workload identity, 교차 앱 거부 시험. 차이: 원천은 환경의 비밀 저장소(Secrets Manager)를 "연결"함. 저장소는 모든 목적지에 같은 금고를 둠. 팀장의 D-23 해석("엔진은 달라도 됨")을 비밀에도 쓰면 EKS 목적지는 Secrets Manager + ESO가 될 수 있음 | **열림, 팀장 결정 필요.** D-23 해석을 비밀에도 적용하나. OpenBao · Vault 안건(`TECH-STACK-SPEC.md` 3절 5번)과 함께 |
| 9 | 관측 · 비용 | OTel + Prometheus 계열 · 로그 · 트레이스, 공급자 비용 자료. 책임: 동일 부하 · 기간 · SLO 기준 비교 | OTel Collector, Prometheus, Tempo, Loki, Grafana(결정). 비용은 NFR-8(component 태그, 예산 알림, 정산 분리)과 스펙 §2.1의 "이전 전후 비용"(얕게). LLM 비용은 게이트웨이가 셈(`07` 6절) | 일치: 관측 스택. 빈 곳: 같은 부하 · 같은 기간 · 같은 SLO에서 출발지와 목적지 비용을 비교하는 설계가 없음. 공급자 비용 자료(AWS CUR, GCP Billing export) 수집도 없음 | **열림, 팀장 결정 필요.** 비교의 범위와 우선순위(데모 3:50 비용 장면에 넣을지) |
| 10 | 백업 · 복구 | 환경별 백업 도구 · 저장소. 책임: 복원 시험 · RPO/RTO · 출발지 없이 복구 가능한지 판정 | 스펙 DR-01: "새 환경으로 실제 복원 성공", CNPG 백업 · 복원(6개 통제의 하나). `11-landing-zone.md` 6절 7번: CNPG 백업은 같은 환경의 S3 호환 저장소로, 다른 환경 복제는 열림 | 해결된 부분: RPO/RTO 측정과 "출발지 없이 복구" 판정을 P1로 더함. 빈 곳: 백업이 같은 환경 저장소에만 있으면 환경을 잃을 때 함께 잃음 | 해결(팀장 결정 11/2, P1) + 열림: 백업 사본을 환경 밖에 둘지 |
| 11 | AI 변경 | Harbor + 모델 공급자 어댑터. 책임: 반출 정책 · 변경 범위 · 독립 시험 · 운영 반영 게이트 | Harbor가 리허설 실행 계층(D-02, 결정). Claude Agent SDK. LLM 게이트웨이(제안: 모델 키 보관, 비용 상한, FQDN 허용 목록). 분리 검증기, 게이트, 사람 승인 | 일치: Harbor, 변경 범위(Trial 토큰의 승인 범위), 독립 시험, 운영 반영 게이트. 빈 곳: "반출 정책"(모델로 보내도 되는 데이터와 마스킹 규칙)이 없음. 게이트웨이가 공급자 어댑터 자리지만 공급자는 하나 | 열림: 반출 정책을 LLM 게이트웨이 계약에 넣을지(GOV-01, D-03과 연결) |
| 12 | 원칙 | "모든 환경에 같은 도구를 설치하는 것은 목표가 아닙니다." 공통 요구와 거부 시험으로 비교하고, 기능이 대응하지 않으면 이전을 제한 | D-23 본문: 같은 묶음을 "같은 판, 같은 이름, 같은 정책으로 설치", "클라우드 관리형 S3 · GCS · Secrets Manager는 목적지 구성요소로 쓰지 않음". 반면 `11-landing-zone.md` 2절과 `TECH-STACK-SPEC.md` 2.3은 객체 저장 엔진을 환경별로 둠(D-22). 동등성을 시험으로 판단하는 것, UNKNOWN BLOCK, 대체 불가는 예외 승인은 원천과 같음 | 해결: 팀장 해석 "같은 계약 · 같은 경험, 엔진은 달라도 됨"은 원천 원칙과 맞음. 남은 것: D-23 본문, `TECH-STACK-SPEC.md` 0절 "어디서나 같은 구조"와 1절 다섯째 줄, `11-landing-zone.md` 1절 "같은 구조"와 5절("RGW에서 RGW로", "금고에서 금고로")이 새 해석과 어긋남 | 해결(팀장 결정 11/2). 문서 정리 필요 |
| 13 | 독창성 1: 보안 · 운영 차이 지도 | 권한 통제 · 암호화 · 감사 · 백업 · AI 데이터 위치를 `충족 / 보완 필요 / 대체 불가 / 미검증`으로 판정 | 게이트 판정 PASS · FAIL · UNKNOWN + 예외 승인(스펙 §8.5, `04-governance.md` 2절). 데이터 위치는 GOV-01 표시만(스펙 §2.1, D-03) | 해결: 콘솔 문구로 대응함. 충족 = 원본 · 목적지 PASS. 보완 필요 = 목적지 FAIL(BLOCK, AI 수정 대상). 대체 불가 = 예외 승인(승인자 · 기간 · 대체 통제, 동등 판정에서 뺌). 미검증 = UNKNOWN(BLOCK). 판정 규칙은 바꾸지 않음. 남은 차이: 원본 · 목적지 모두 FAIL인 칸(스펙은 실패로 셈)도 "보완 필요"로 보임. AI 데이터 위치는 원천에서 판정 대상, 스펙에서 표시만 | 해결(팀장 결정 11/2). AI 데이터 위치를 판정에 넣을지는 D-03b와 함께 열림 |
| 14 | 독창성 2: 차이를 해소하는 AI 샌드박스 루프 | AI는 자기가 고친 테스트나 정책만으로 통과를 선언하지 못함. 독립 검증 결과가 운영 반영을 정함 | FR-5 비공개 테스트 · 별도 검증 Job. D-02 분리 검증기. 검증기는 산출물을 import하지 않음(`platform/rehearsal/tests/test_verifier.py`). 결과는 controller 신원 · 검증 Pod UID · 시도 번호에 묶음(`05-agent-rehearsal.md` 3절) | 일치 | 아니오 |
| 15 | 독창성 3: 출발지 의존 차단 리허설 | 목적지 검증 환경에서 출발지 접근을 차단해 숨은 의존성을 확인. 실패하면 의존성 지도와 이전 계획을 갱신. "앱 전환 완료"와 "출발 환경 없이 운영 가능"을 구분 | FR-15 Exit Drill P1(T27): 목적지 앱의 출발지 접근을 끊고 재시작 · 새 연결까지 시험, 복제 채널 유지. 잔존 의존성 지도는 DONE 증거표의 P1 항목(`03-migration.md` 5절). DONE 조건은 "WATCH에서 통제 재시험 PASS" 하나 | 차이 셋: (a) 원천에서는 독창성의 핵심, 저장소에서는 P1 (b) 원천은 컷오버 전 검증 환경에서 함. 스펙은 목적지 앱에서 하며 시점을 정하지 않음 (c) 원천은 완료 상태를 둘로 나눔. 저장소는 DONE 하나 | **열림, 팀장 결정 필요.** P0으로 올릴지, 리허설 단계에 넣을지, DONE 보고서에 "출발 환경 없이 운영 가능" 칸을 둘지 |
| 16 | 제품 이름 | 원천은 플랫폼을 "Sova"로 부름 | 스펙 머리말 "제품명 미정(12장)". 저장소는 "Bravo 플랫폼" | 차이 | 열림: 제품 이름(스펙 §12) |

## 2. 오늘 팀장 결정과 아직 고칠 문서

이 표의 문서는 다른 에이전트가 작업 중이라 이번에 고치지 않았습니다. 판정 규칙을 바꾸는 결정은 없습니다.

| 팀장 결정(2026-11-02) | 1절 행 | 이미 반영된 곳 | 아직 옛 내용인 곳 | 기록할 곳 |
|---|---|---|---|---|
| Cilium + Hubble을 모든 환경에. Tetragon은 P1 | 6 | `TECH-STACK-SPEC.md` 1절 · 2.2 · 4절, `11-landing-zone.md` 2 · 3절 | `09-cni-csi.md`(원칙 1, 결정 표, 2절, 4절 대안), `TECH-STACK-SPEC.md` 2.2 Istio 행의 대안 칸, D-15 | 구현 선택이라 `06-tech-stack.md`와 ADR. 스펙 결정은 바꾸지 않음 |
| Cloudflare 전역 가중치 + 환경별 Gateway API(Envoy, Cilium 구현) | 3 | `TECH-STACK-SPEC.md` 1절, `11-landing-zone.md` 2절 | `02-network.md`(흐름 표, 6절 서비스 노출에 Gateway 없음), `07` 4절("Gateway API CRD는 필요 없음"은 Istio waypoint 기준. Cilium Gateway API는 CRD가 필요) | FR-8은 그대로. ADR |
| D-23 = 같은 계약 · 같은 경험, 엔진은 달라도 됨 | 12 | `TECH-STACK-SPEC.md` 2.3(객체 저장 엔진 환경별), `11-landing-zone.md` 2절 표 | `DEVIATIONS.md` D-23 본문과 표, `11-landing-zone.md` 1 · 5 · 6절, `TECH-STACK-SPEC.md` 0절 · 1절 | D-23 본문을 고침 |
| OPA를 Kyverno와 함께 | 7 | `TECH-STACK-SPEC.md` 1절 · 2.2, `11-landing-zone.md` 2절 | `09-cni-csi.md` 2절(Kyverno만) | `06-tech-stack.md` |
| 4상태 문구(충족 / 보완 필요 / 대체 불가 / 미검증)를 PASS · FAIL · UNKNOWN · 예외에 대응 | 13 | 없음 | `04-governance.md` 2절, 콘솔(FR-13) 문구 | 화면 문구라 편차 아님. `04-governance.md`에 대응표 |
| 백업 · 복구 시험(RPO/RTO, 출발지 없이 복구)은 P1 | 10 | 없음 | `SPEC-TRACE.md`(DR-01 확장 P1 행), `04-governance.md` DR-01 하위 시험 | `DEVIATIONS.md`(스펙 DR-01보다 넓음) |
| ECS · VM 어댑터는 P2 | 2 | 없음 | `SPEC-TRACE.md`, 스펙 §4 비목표와의 관계 | `DEVIATIONS.md`(범위 추가, P2) |

## 3. 나머지 1급 원천

### 3.1 결정적 워크플로우 × 비결정적 판단 구현안(배규태)

원천: [sources/deterministic-workflow.md](sources/deterministic-workflow.md). 원문이 스스로 "(참고용)", "결정 전 초안"이라고 적습니다.

| 항목 | 원천 | 현재 결정 | 판정 | 결정 필요 여부 |
|---|---|---|---|---|
| 설계 원칙 | 에이전트는 제안서만, 실행 · 판정은 파이프라인 | 스펙 §8.1, NFR-5, `AGENTS.md` | 일치 | 아니오 |
| ⑥ 실행 도구 | Argo · Helm(Argo CD/Rollouts)로 컷오버 · 롤백 | Executor를 직접 만듦. Argo Rollouts는 클러스터를 넘지 못해 P2 비목표(스펙 §4, §8.7). Argo CD는 배포만(제안 D-16) | 차이. 스펙이 이미 바꿈 | 아니오(정본 우선) |
| ⑦ 사후 | 임계치를 넘으면 롤백. PoC는 수동 롤백 버튼 | SHIFT 중 가중치 자동 0(FR-8). 컷오버 뒤 되돌리기는 사람이 콘솔에서 시작(D-18) | PoC 범위와 일치 | 아니오 |
| 상태 머신 | 재시도 최대 3회, 넘으면 "사람 확인 필요" | 스펙 §8.2, FR-5, NEEDS_HUMAN | 일치 | 아니오 |
| 제안서 계약 | JSON(step, source, target, proposal, rationale, risk). 스키마 검증 → 정책 린트 → 샌드박스 → 게이트 | 도구 계층의 구조화 작업(D-06), 리허설 프로토타입 | 대체로 일치. 필드 이름은 대조하지 않음 | 아니오(에이전트 담당이 확인) |
| 번역 예시 | 대상이 MinIO 정책 | 객체 저장은 같은 S3 계약, 온프렘 Rook-Ceph RGW(D-22). MinIO는 제외 | 차이. 저장소가 대체 | 아니오 |
| 가드레일 6개 | 스키마, 실행 권한 없음, 도구 계층 차단, 재시도 상한, 판정 · 실행에 LLM 금지, 전부 기록 | `AGENTS.md` "코드만 봐서는 모르는 것", 도구 계층 시험. "리플레이"는 `07` 10절에서 "기록과 재채점"으로 고쳐 씀 | 일치 | 아니오 |
| 게이트 질문(PoC) | 저장소 접근, 행 수 · 체크섬, 시퀀스 | 스펙: 행 수 · 해시 · 시퀀스는 게이트가 아니라 컷오버 5단계의 최종 비교 | 차이. 스펙이 이미 바꿈 | 아니오(정본 우선) |
| 팀이 정할 것 | Agent SDK, Harbor 역할, 상태 머신 직접 또는 엔진, 승인 화면 위치 | Claude Agent SDK(스펙), Harbor 실행 계층(D-02), 상태 머신 직접 제작, 승인은 콘솔만(D-18, Slack 없음) | 모두 정해짐 | 아니오 |

### 3.2 Harbor 도입 관련(배규태)

원천: [sources/harbor-adoption.md](sources/harbor-adoption.md)

| 항목 | 원천 | 현재 결정 | 판정 | 결정 필요 여부 |
|---|---|---|---|---|
| 도입 방안 | 방안 1 추천: 런타임 샌드박스는 직접, Harbor는 평가만 | 스펙 §8.4는 방안 1(Harbor 평가 전용 P1). 저장소는 D-02로 리허설 1회 = Harbor Trial 1회(방안 2 쪽). 11/4 18:00 되돌림 기준을 못 넘으면 방안 1로 돌아감 | 차이. 팀장 결정(D-02)으로 정해짐. 작성자 우려는 `07` 10절이 답함 | 아니오. 회의 공유는 필요 |
| 에이전트 네트워크 | LLM API만 허용 | 에이전트 → LLM 게이트웨이만. 바깥 이름 허용 목록은 게이트웨이에 | 일치(경유 지점만 다름) | 아니오 |
| 검증기 | 분리된 샌드박스, 네트워크 없음 | 검증 네임스페이스 분리. 검증기는 목적지 시험 대상에 닿아야 하므로 네트워크가 있음(D-12 예외) | 차이(이유 있음) | 아니오 |
| 정적 작업 구조 | 작업 템플릿 + 스냅샷 주입 | Harbor 환경 어댑터(T15a) | 일치 | 아니오 |
| 로컬 Docker 허용 목록 조건 | 리눅스 컨테이너 + nftables 필요 | Docker가 아니라 K8s 네임스페이스 어댑터. 허용 목록은 NetworkPolicy(Cilium) | 차이. 이 문제가 생기지 않음 | 아니오 |
| "리플레이" 용어 | 발표 문구를 맞춤 | `07` 10절: 발표에서 "리플레이"라고 하지 않음 | 일치 | 아니오 |
| Trial 1회 시간 측정 | 로컬에서 먼저 잼 | D-02 되돌림 기준(11/4 18:00)에 시간 항목이 있는지 확인하지 못함 | 빈 곳 | 열림(작음): Trial 시간 실측을 T15a 완료 기준에 넣을지 |
| 평가용 결함 시나리오 | 목록 확정 | T26 | 일치 | 아니오 |

### 3.3 문제정의/타겟/시나리오(배규태)

원천: [sources/problem-target-scenario-bae.md](sources/problem-target-scenario-bae.md)

| 항목 | 원천 | 현재 결정 | 판정 | 결정 필요 여부 |
|---|---|---|---|---|
| 문제 정의 | 동등성을 보장하는 도구가 없음, 나가는 방향 도구가 비어 있음 | 스펙 §2 | 일치 | 아니오 |
| 타겟 | 후보 A(금융 · 의료 플랫폼팀), 후보 B(금융권 설치형 SaaS) | 스펙 §3: 금융사 플랫폼 · 인프라 운영팀, 일본 금융 서사 우선. 설치형 SaaS는 확장 대상 | 스펙이 A의 금융만 받음 | 아니오 |
| 규제 기준선 | 한국 전자금융감독규정 시행세칙 별표7 | 스펙: 일본 금융청 가이드라인 우선, 한국은 보조. 규제는 매핑표로만(§8.5) | 차이. 스펙이 바꿈 | 아니오 |
| 출발지 | AWS 앱 + RDS PostgreSQL + S3 + KMS + IAM | 출발지 EKS + CNPG. RDS는 비목표 P2(열린 안건 2번) | 차이 | 열림(기존 안건 2번) |
| 목적지 | 온프렘 VM 위 K3s + PostgreSQL + MinIO + Vault | K3s(D-21로 원천과 같아짐) + CNPG + Rook-Ceph RGW(D-22) + OpenBao(Vault 전환 열림) | K3s 일치, 나머지 차이 | 열림: OpenBao 또는 Vault(`TECH-STACK-SPEC.md` 3절 5번) |
| ③ 버킷 삭제 시도 | 도구 계층이 차단하고 기록 | S2 장면 | 일치 | 아니오 |
| ⑤ 원본 대조 | 행 수 · 체크섬을 게이트에서 봄 | 데이터 동일성은 컷오버 5단계 | 차이. 스펙이 바꿈 | 아니오(정본 우선) |
| ⑧ 트래픽 전환 | Argo Rollouts | Cloudflare LB 가중치(FR-8). Argo Rollouts는 P2 비목표 | 차이. 스펙이 바꿈 | 아니오(정본 우선) |
| ⑨ 원본 보존 | AWS를 롤백용으로 보존 | 되돌리기(FR-11), 역방향 복제 | 일치 | 아니오 |

### 3.4 설계 추가 제안

원천: [sources/design-additional-proposals.md](sources/design-additional-proposals.md)

| 항목 | 원천 | 현재 결정 | 판정 | 결정 필요 여부 |
|---|---|---|---|---|
| 2절 효과 원장 · 인계 차단 | 효과 상태 5개, 미해결이면 인계 차단, 조회 전용 복구 | D-19(제안). `platform/executor/README.md`가 상태 5개를 그대로 씀 | 일치(제안 상태) | 열림: D-19 채택(회의) |
| 3절 독립 보안 보류 · 별도 해제 | 일반 승인으로 풀리지 않는 보류, revision 재확인, 기한 지나도 유지 | D-20(제안). `04-governance.md` 보류 절, `platform/executor/README.md` 5절 | 일치(제안 상태) | 열림: D-20 채택(회의) |
| 4절 첨부 이전 · 역방향 복구 | 논리 객체 ID, 새 버전 생성, 내용 digest 참조, outbox, 최종 전환 조건 7개 | D-04(제안)가 이 지원 계약을 받아 고침. 객체 키는 내용 SHA-256, 별도 outbox 대신 첨부 행 테이블, 동결은 DB 잠금으로 직렬화 | 대체로 일치. 차이: 원천의 "버전 생성 · 버전 참조"를 저장소는 내용 주소 키로 바꿈. 원천의 기준 저장소는 SeaweedFS(스펙), 저장소는 RGW(D-22) | 열림: D-04 채택(회의) |
| 5절 검증 환경 접근 통제 | broker의 exec 허가를 Pod UID까지 묶음, 실행 · 검증 네임스페이스 분리, 산출물 검사 | `05-agent-rehearsal.md` 3절(허가 결속, 결과 결속, V3 · V3b 시험), `07` 3절 | 일치 | 아니오 |
| 6절 반영 순서 | 효과 원장 → 보류 → 검증 접근 → 첨부 | 일정(`docs/plans/2026-11-03-to-07.md`)과 대조하지 않음 | 미확인 | 열림(작음): 일정 순서 확인 |

## 4. 팀장 결정이 필요한 것

중요한 순서입니다.

1. **스펙 정본이 바뀌었는지.** Notion의 스펙 첨부 HTML이 사본(04:52) 뒤 15:50–16:03 KST에 세 번 바뀌었습니다(`sources/spec-v0.1.md`). 내용은 비교하지 못했습니다. 새 첨부를 받아 새 사본으로 둘지 정해 주세요. 받기 전까지 이 정렬표는 04:52 판을 기준으로 합니다.
2. **출발지 의존 차단 리허설(1절 15행).** 원천은 독창성의 핵심으로 두고, 저장소는 FR-15 Exit Drill P1입니다. P0으로 올릴지, 컷오버 전 리허설에 넣을지, DONE 보고서에 "출발 환경 없이 운영 가능" 칸을 둘지.
3. **D-23 해석을 비밀에도 적용하는지(1절 8행).** EKS 목적지에서 Secrets Manager + ESO를 허용할지, 모든 목적지에 같은 금고를 둘지. OpenBao · Vault 안건과 함께.
4. **비용 비교(1절 9행).** 같은 부하 · 기간 · SLO에서 출발지와 목적지 비용을 비교할지, 데모에 넣을지.
5. **공급자 보안 이벤트 연결(1절 6행).** GuardDuty 같은 공급자 탐지를 통제 ID로 정규화하는 경로의 우선순위.
6. **Istio ambient(D-15)와 Cilium(1절 6a행).** 11/4 판정 전에 대안 비교를 고칠지.
7. **백업 사본의 위치(1절 10행).** 환경 밖에 백업을 둘지.
8. **반출 정책(1절 11행).** 모델로 보내는 데이터의 규칙을 LLM 게이트웨이 계약에 넣을지.
9. **제품 이름(1절 16행).** 원천의 "Sova"를 쓸지.
10. **1급 지정 확인.** 「결정적 워크플로우 구현안」과 「Harbor 도입 관련」은 원문 제목이 "(참고용)"입니다. 1급으로 둘지 다시 확인해 주세요. 이 둘의 여러 행은 스펙 v0.1이 이미 바꿨습니다(3.1, 3.2절).
