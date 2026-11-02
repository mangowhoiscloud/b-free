# 11/3 회의: 운영 중인 "제안" 편차와 스펙 → 구현 대응표

**기준 시각: 2026-11-03 02:40 KST.** 문서 갈래가 만든 회의 자료입니다. 팀 결정이 아닙니다. 아래 "ADR 제안" 번호는 비어 있는 다음 번호를 미리 잡아 둔 것입니다(지금 ADR은 0001–0010).

**왜 필요한가.** `DEVIATIONS.md`는 "회의에서 정하기 전에는 구현하지 않는다"고 썼습니다. 그러나 "제안" 상태 편차 여럿이 이미 운영 환경에서 돕니다. 스펙이 정본인데 스펙과 다른 것이 돌면, 심사위원의 "스펙과 코드가 다르면 무엇이 이기나"에 답할 수 없습니다(리뷰 P19, 심사위원 페르소나 J4-2). 이 회의에서 하나씩 채택 · 기각을 정하고, 채택하면 ADR을 쓰고 스펙 v0.2에 넣습니다. 구현이 먼저였다는 사실은 그대로 적습니다("구현 선행, 회의 확인").

증거의 기준 관측: `docs/evidence/cloud-runs/platform-snapshot-20261103T0232KST.txt`(플랫폼 DB에 계획 · 승인 · 영수증 0건, `shift` run 6건).

## 1. 정할 것: "제안" 상태로 운영 중인 편차

| ID | 무엇이 도나 | 증거 | 정할 것 | ADR 제안 |
|---|---|---|---|---|
| D-01 GKE 목적지 | GKE `bravo-target-gcp`에 같은 랜딩 존, 앱 `b-free-chat-gke`, 게이트 쌍 `source-aws ↔ target-gcp`, 수집기 읽기. 게이트는 OBJ-06(평문 HTTP 허용)으로 늘 BLOCK. LB pool · Executor topology · T31 반영 경로에는 없음. 앱 digest가 출발지 · 온프렘과 다름 | `docs/evidence/gate/platform-gate-20261102T170251Z/`, `…/obj06-gke-orgpolicy-20261102T143014Z/`, snapshot | 범위를 "통제 비교표 셋째 열 + S1 5b 예외 장면, 컷오버 없음"으로 채택할지. GKE 컷오버를 범위에 넣을지(넣으면 LB 요금제 · topology · digest 필요). 2일분 비용 상한 | 0011(ADR 0004의 GKE 부분을 대체) |
| D-04 첨부 이전 | Executor의 첨부 동결 · 복사 · 대조 코드(`attachment_store`, `attachment_copies`, 영수증 `verdict_parts`). 클라우드에서 단독 복사 1회(첨부 3개, S3 → RGW, SHA-256 대조, 두 앞단에서 읽기) | `docs/evidence/cloud-runs/cloud-t8-attachments-20261102T225707/`, `attachment_copies` 3행 | 채택 여부. NFR-1의 5초를 DB 쓰기 정지에만 적용할지. 영수증 MATCH에 첨부를 넣을지(지금은 미검사면 `MATCH_PARTIAL`). 스펙 r2 T8(컷오버 직전 1회)보다 넓은 부분(동결, WATCH 역복사)을 P0에 둘지 | 0012 |
| D-05 수집기 작업 | 수집기 Deployment(`collector@sha256:af3c66c1…`)가 세 클러스터를 읽음(`GET /v1/clusters`, `GET /v1/inventory`) | `docs/evidence/d16-source-k8s/` | 담당과 작업 번호. 제안의 "T30"은 스펙 r2의 T30(목적지 워크로드)과 겹침 | ADR 없이 `SPEC-TRACE.md`에 새 작업 번호 |
| D-09 컷오버 6a · 6b | Executor 단계 `6a_switch_replication` · `6b_commit_point`가 배포된 이미지에 있음. 클라우드 컷오버는 0회 | `platform/executor/src/bravo_executor/cutover.py`, `docs/evidence/pilot-notebook/`(파일럿) | 채택하고 스펙 v0.2 §8.6에 넣을지. `FAILBACK_REQUIRED` 결과 값은 아직 없음 | 0013(D-19와 함께) |
| D-10 가중치 API · LIVE/RECORDED | Executor `POST /v1/front/shift`가 LB 가중치 10 → 50 → 100%, 단계 판정, 실패 시 0% 복귀. 하네스로 6회(DONE 4, 자동 복귀 2). 계획 API · 콘솔 버튼 · `mode: recorded` 없음 | `docs/evidence/cloud-runs/cloud-front-*` | API 모양(`/v1/plans/{id}/weights` 또는 상태 머신 SHIFT)과 콘솔 버튼 담당 · 기한. `recorded` 재생을 넣을지. 대표 CNAME 0/100은 FR-8 대체가 아님(D-10 본문)을 확인 | 0014 |
| D-11 예외 승인 | `/v1/plans/{id}/exceptions`(승인 API와 별도 표), 마이그레이션 0008 운영 DB에 적용. 사용 0건 | `docs/evidence/control-exception/`, snapshot(`control_exceptions` 0행) | 별도 경로로 채택할지. 대체 통제를 시험하는 게이트 질문을 예외 조건으로 둘지(리뷰 P10) | 0015 |
| D-12 리허설 NetworkPolicy 예외 | 리허설 서비스(`rehearsal@sha256:1e1337fa…`)의 검증 Job이 RGW 송신 허용 규칙을 만듦. Trial 1회 PASS(노트북 CLI 러너) | `docs/evidence/s1-s2-bucket/`, `platform/rehearsal/src/bravo_rehearsal/behavioral.py` | 예외 목록(kube-dns, 시험 버킷, 검증 네임스페이스)과 예외별 격리 시험을 채택할지 | 0016 |
| D-16 Cloudflare 링크 | 환경 사이 유일한 경로. 플랫폼 → 세 클러스터 API · DB · PgBouncer · 관리 포트 · RGW, 고객 → 플랫폼 OpenBao · OTel. Tailscale 없음 | `docs/evidence/d16-source-k8s/`, `cloud-runs/cloud-executor0-checks-*`, `cloud-runs/cloud-repl-eks-rke2-*`, `docs/evidence/repl-timeouts/` | ADR 0003을 "채택"으로 바꿀지. 링크 끊김 합격선(복제 타임아웃 60초로 바꿈). 서비스 토큰 만료 11/9 20:50 KST 전 회전 연습(11/6). Cloudflare 단일 장애점을 발표에서 어떻게 말할지 | 0003(상태 변경) |
| D-19 효과 원장 | Executor run마다 효과 행(`CONFIRMED_APPLIED` 등)을 남김. 앞단 전환 run에 `WEIGHT_DST_*`, `REVERT_DST_0` 행 | `cloud-runs/cloud-front-*/…/shift-run-to-dst.json`의 `effects` | 채택할지(팀원 메모가 출처). 인계 차단 규칙 포함 여부 | 0013(D-09와 함께) |
| D-25 상태 경로 구현 선택 | 플랫폼 API(`api@sha256:59f6ba2b…`)의 ANALYZE 분기, WATCH 재시험, NEEDS_HUMAN 재개, 도구 이름 | `platform/api/src/bravo_api/statemachine.py`, 단위 시험. 운영 계획 0건이라 실행 확인 없음 | 채택할지. SHIFT 전이가 없는 것(리뷰 B02)을 이 편차에서 함께 다룰지 | 0017 |
| D-28 빌드 · 서명 · 배포 경로 | 모든 배포: 플랫폼 서비스는 운영자 buildx → ECR, 데모 앱은 출발지 ECR · 온프렘 containerd import(같은 digest) · GKE Artifact Registry(다른 digest). T31은 플랫폼 빌드 → OpenBao transit 서명 → digest 대조 → import. Actions → GHCR는 게시만 | `docs/evidence/t31/t31-20261102T120416Z/`, snapshot, `DEVIATIONS.md` D-28 | 채택하고 스펙 §7.2 · §8.8 · T7 · T31 문장을 바꿀지. T7 완료 기준("Actions OIDC 배포 1회")을 이 편차로 대체할지 | 0018 |

## 2. 참고: "제안"이지만 운영하지 않는 편차

| ID | 지금 | 회의에서 |
|---|---|---|
| D-03 거버넌스 팩 | 매핑표 파일 · 사전 심사 코드 없음 | P1로 둘지 |
| D-07 리전 이전 | 설계 문서에만 | 그대로 |
| D-08 DONE 보고서 | 보고서 코드 없음 | P1로 둘지 |
| D-13 승인 요약 템플릿 | 코드 템플릿 없음 | P0 범위에서 뺄지 |
| D-14 GHCR 공개 패키지 | CI가 GHCR에 올리지만 어느 클러스터도 GHCR에서 받지 않음 | D-28과 함께 정리 |
| D-15 Istio ambient | 플랫폼 RKE2에 `istio-system` 없음(02:3x). 되돌림 기준(11/4 12:00)은 플랫폼 EKS 시절 문장 | 기각 또는 보류 |
| D-20 보안 보류 | API가 `security_holds: null`, `not_implemented:D-20` | P1로 둘지 |
| D-24 관리형 PostgreSQL | 노트북 시험(`deploy/pilot/runs/spike-*`, Git 제외)만. 운영 경로 아님 | 범위 결정 |

## 3. 참고: 팀장 결정 · 지시였고 회의 공유만 필요한 편차

| ID | ADR | 회의에서 확인할 것 |
|---|---|---|
| D-02 Harbor 런타임 | 0002 | 11/4 18:00 되돌림 판정을 노트북 CLI가 아니라 플랫폼 리허설 서비스 경로로 할지(`rehearsals` 0행) |
| D-06 도구 계층 작업 목록 | 없음 | 구현 선택으로 둘지 |
| D-18 운영자 MCP | 없음 | ADR 0019 제안. 270초 장면에 넣을지 |
| D-21 · ADR 0010 RKE2 | 0010 | 확인만 |
| D-22 RGW | 0005 | 기존 객체 5개 평문(리뷰 B13) 처리 |
| D-23 같은 랜딩 존 | 0006 | 확인만 |
| D-26 키는 플랫폼 금고 | 0007 | 확인만. D-28의 이유 |
| D-27 역할별 에이전트 여섯 | 없음 | ADR 0020 제안. 실행 기록이 governance 하나뿐인데 발표에서 여섯을 말할지 |

## 4. 회의에서 함께 정할 것

1. 정본 규칙: 스펙과 코드가 다르면 스펙이 이깁니다. 편차는 ADR로 채택된 뒤에만 코드가 이깁니다. 이 규칙을 다시 확인할지.
2. 스펙 v0.2를 누가 언제 낼지. 11/7 10:00 제출 전에 내지 못하면 설계 문서 앞에 아래 5절 대응표를 둡니다.
3. 무대의 승인자: 지금 operator(naver)와 approver(gmail)는 모두 팀장 메일입니다. 다른 팀원이 approver가 될지(리뷰 P8).

## 5. 스펙 → 구현 대응표

상태 칸의 말뜻: **클라우드 실측**은 실제 환경에서 플랫폼 서비스가 돈 기록이 있음. **하네스** · **노트북**은 스크립트 직접 호출 · 운영자 노트북 실행. **코드만**은 배포됐지만 운영 실행 기록 없음. **없음**은 실행 기록 0건. 플랫폼 DB에 계획이 0건이므로, 콘솔 → API → 상태 머신을 거친 실행은 어느 행에도 없습니다.

### 5.1 기능 요구

| 스펙 | 코드 | 증거 | 상태 |
|---|---|---|---|
| FR-1 읽기 전용 수집 | `platform/collector/` | `docs/evidence/d16-source-k8s/` | 부분(클러스터 읽기 실측). S3 정책 · IAM · KMS 수집을 계획에 묶은 기록 없음 |
| FR-2 의존성 3등급 | `platform/collector/`(인벤토리), `platform/agent/` | `d16-source-k8s/README.md`(파일 40, 규칙 판) | 부분. 계획에 묶인 분석 0건 |
| FR-3 목적지 구축(RKE2 CIS, 공통 cloud-init) | `infra/modules/cluster/onprem/cloud-init`, `infra/envs/target-onprem`, `deploy/landing-zone/` | `docs/evidence/s1-cis-refusal/`, `traefik-crd-skip/` | 부분. 빈 VM 구축은 노트북 Lima에서 실측. 운영 2노드 목적지의 구축 기록 연결 없음 |
| FR-4 AI 번역 · 수정안 | `platform/agent/`, `platform/rehearsal/` | `docs/evidence/s1-s2-bucket/` | 부분(버킷 정책 작업 1건). Secrets Manager 코드 수정 작업 0건 |
| FR-5 리허설 · 분리 검증 · 3회 | `platform/rehearsal/`(`behavioral.py`, `tool_layer.py`) | `s1-s2-bucket/`, `platform/rehearsal/docs/evidence/` | 노트북(CLI 러너). 플랫폼 리허설 서비스 경로 0회 |
| FR-6 원본 대조 게이트 | `platform/gate/` | `docs/evidence/gate/platform-gate-20261102T170251Z/` | 클라우드 실측(게이트 서비스 단독). 계획 상태 차단까지 이은 기록 없음 |
| FR-7 승인 결속 | `platform/api/`(승인), `platform/apply/` | `docs/evidence/t31/`(CLI, `approval_id: null`) | 코드만. 승인 0건 |
| FR-8 가중치 10/50/100, 0% 복귀 | `platform/executor/src/bravo_executor/front.py`, `infra/modules/edge/cloudflare/` | `docs/evidence/cloud-runs/cloud-front-*` | 하네스(이동 4회, 복귀 2회). 상태 머신 SHIFT · 콘솔 시작 없음. 판정 표본은 설정 조회 프로브 |
| FR-9 논리 복제 · PAUSE · 펜싱 · barrier | `platform/executor/`(`cutover.py`, `pools.py`), `deploy/cloud/repl_setup.sh` | `cloud-runs/cloud-repl-eks-rke2-*`, `cloud-runs/cloud-executor0-checks-*`(E4), `pilot-notebook/` | 복제 · E4는 클라우드 실측. 컷오버 0회 |
| FR-10 영수증 | `platform/executor/`(`verdict_parts`) | `pilot-notebook/`(파일럿) | 없음(클라우드 영수증 0건) |
| FR-11 데이터째 되돌리기 | `platform/executor/`(failback) | `pilot-notebook/`(파일럿) | 없음 |
| FR-12 통제 전후 비교 | `platform/gate/` | `gate/platform-gate-*`(이전 전만) | 부분. 컷오버 뒤 재시험 없음 |
| FR-13 콘솔 | `platform/console/` | `d16-source-k8s/*.png`(클러스터 화면) | 부분. 로그인 뒤 전체 흐름 0회 |
| FR-14 수정 불가 감사 기록(P1) | `platform/audit/` | 없음 | 코드만 |
| FR-15 Exit Drill | `deploy/cloud/exit_drill.sh` | 없음 | 없음 |

### 5.2 비기능 요구

| 스펙 | 코드 | 증거 | 상태 |
|---|---|---|---|
| NFR-1 쓰기 정지 5초, 부하 5회 | `platform/executor/`, `tools/loadgen/` | `pilot-notebook/`(76–139 ms, 파일럿) | 없음(클라우드). 부하 생성기는 쓰기 20/s만 |
| NFR-2 유실 · 중복 0 | `tools/loadgen/`(`stats.py`) | `cloud-runs/cloud-20261102T225937-cutover/`, `…230116-cutover/` | 부분. 부하 대조는 클라우드 실측, 컷오버 없는 구간 |
| NFR-3 SAFE_HOLD | `platform/executor/` | 없음 | 코드만(E1 · E5 · E6 미실행) |
| NFR-4 에이전트 격리 | `platform/rehearsal/`, 리허설 NetworkPolicy | `s1-s2-bucket/`(거부 2건은 probe) | 노트북(CLI 러너). 최근 Trial Hubble DROPPED 0 |
| NFR-5 결정성 | 모듈 분리(`platform/gate`, `platform/executor`, `platform/api`) | 없음 | 부분. 금지 import 검사 없음 |
| NFR-6 장기 키 없음 | 플랫폼 OpenBao(D-26), `deploy/pilot/scripts/keys.sh` | `gate/obj-enc-onprem-*`, gitleaks | 부분. 링크 서비스 토큰은 7일짜리 |
| NFR-7 IaC 생성 · 삭제 | `infra/` | `s1-cis-refusal/`(버리는 VM) | 부분. 전체 환경 재구축 시간 없음 |
| NFR-8 비용 태그 · 보고 | `infra/`, 수집기 비용 | 커밋 `dfdff7c`(청구가 태그를 아직 못 봄) | 부분 |

### 5.3 시나리오와 작업

| 스펙 | 코드 | 증거 | 상태 |
|---|---|---|---|
| S1(등록 → 수집 → 구축 → 게이트 BLOCK) | 수집기, 게이트, `deploy/cloud/rgw_regression.py` | `gate/platform-gate-20261102T170251Z/`, `s1-s2-bucket/`, `s1-cis-refusal/` | 부분. 게이트 BLOCK은 실측. 계획 0건 |
| S2(리허설 → 승인 → 반영 → 재대조 PASS) | 리허설, `platform/apply/` | `s1-s2-bucket/`, `t31/` | 노트북. 승인 · 반영 · 재대조 PASS 없음 |
| S3(10/50/100 → 컷오버 → 영수증) | Executor | `cloud-runs/` | 앞단만 하네스. 컷오버 0회 |
| S4(장애 → 승인 → 되돌리기) | Executor, `deploy/cloud/fault.sh`, `t25.sh` | `cloud-runs/cloud-front-20261102T225316-*/error/`(앞단 수준 장애만) | 없음 |
| T2 목적지 RKE2 CIS | `infra/modules/cluster/onprem/cloud-init` | `s1-cis-refusal/`, `kube-bench/` | 부분. 운영 목적지 kube-bench는 게이트 갈래 진행 중 |
| T7 플랫폼 EC2 + RKE2, Actions OIDC | `infra/`, `deploy/platform/` | snapshot | 플랫폼은 클라우드 실측. Actions OIDC 배포 0회(D-28) |
| T8 첨부 동기화 | `platform/executor/`(`attachment_copy`) | `cloud-runs/cloud-t8-attachments-*` | 단독 1회(3개). 컷오버 안에서 0회 |
| T9 OpenBao · ESO · 객체 저장 | `deploy/platform/openbao/`, `deploy/landing-zone/` | `gate/obj-enc-onprem-*` | 부분. 온프렘 ESO Ready 아님(값은 파이프 전달). Secrets Manager → OpenBao 이관 0회 |
| T12 컷오버 Executor | `platform/executor/` | 없음(클라우드) | 코드만 |
| T14 통제 시험 | `platform/gate/` | `gate/` | 부분. kube-bench · Prowler · gitleaks 등 스펙 시험과 게이트 질문 대응 없음(리뷰 B12) |
| T15 리허설 | `platform/rehearsal/` | `s1-s2-bucket/` | 노트북 |
| T17 Edge 가중치 | `front.py` | `cloud-runs/cloud-front-*` | 하네스 |
| T18 부하 · 관측 | `tools/loadgen/`, `deploy/platform/observability/` | `cloud-runs/`(20/s 쓰기) | 부분. 50 · 150 req/s 없음 |
| T19 게이트 | `platform/gate/` | `gate/` | 클라우드 실측 |
| T20 되돌리기 | `platform/executor/` | 없음 | 코드만 |
| T21 콘솔 | `platform/console/` | `d16-source-k8s/*.png` | 부분 |
| T24 감사 저장소(P1) | `platform/audit/` | 없음 | 코드만 |
| T25 장애 주입 | `deploy/cloud/fault.sh`, `t25.sh` | `cloud-runs/cloud-front-20261102T225316-*/error/` | 부분. `t25.sh fault`는 컷오버와 되돌리기까지 부름(리뷰 B03) |
| T27 Exit Drill | `deploy/cloud/exit_drill.sh` | 없음 | 없음 |
| T28 통합 리허설 3회 | `docs/demo/SCRIPT.md`, `INTEGRATION-CHECK.md` | 없음 | 없음 |
| T30 목적지 워크로드 | `deploy/landing-zone/`, `apps/demo/` | snapshot(출발지 · 온프렘 같은 digest, 세 앞단 200) | 클라우드 실측 |
| T31 승인 반영 경로 | `platform/apply/` | `t31/`(CLI 1회, 그때 K3s) | 노트북. 콘솔 승인 · 정책 적용 · 재대조 PASS 없음 |
