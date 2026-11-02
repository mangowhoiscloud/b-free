# Bravo 플랫폼

Team Bravo의 클라우드 출구전략 플랫폼입니다(SoftBank Hackathon 2026 본선). 가동 중인 서비스를 한 고객 클러스터에서 다른 고객 클러스터로 옮깁니다. 옮기기 전에는 목적지의 보안 통제가 원본과 같은지 비교하고, 옮기는 동안에는 데이터 유실과 중복이 없음을 증명합니다. 문제가 생기면 그사이 쓴 데이터까지 들고 되돌립니다.

**상태(2026-11-02).** 구현 전입니다. 문서와 폴더 구조만 있습니다. 예외는 `platform/rehearsal/`이며, 리허설 프로토타입의 코드와 시험이 들어 있습니다.

## 깊이 하나

가동 중인 서비스를 멈추지 않고 옮기고, 옮긴 뒤에도 같다는 것을 증명하고, 문제가 생기면 데이터째 되돌립니다. 나머지 기능은 이 문장을 증명하는 수단입니다.

## 정본

스펙 v0.1이 이 저장소의 1급 기준입니다. 저장소 문서는 "스펙을 어떻게 구현하나"만 적습니다. 규칙은 [docs/spec/README.md](docs/spec/README.md)에 있습니다.

## 환경 넷

플랫폼 클러스터 하나와 고객 앱 환경 셋입니다.

| 환경 | 역할 | 위치 | 구성 | 근거 |
|---|---|---|---|---|
| `platform-aws` | 중앙 제어(본부) | AWS 서울 3-AZ, VPC ② | Amazon EKS(제어면은 AWS 관리). 관리형 노드 그룹 core 3대(AZ마다 1) + agents 1대(taint) | 스펙 §7.2(EC2 단일 노드)와 다름. 결정 D-21 |
| `source-aws` | 고객 앱 출발지 | AWS 서울, VPC ① | EKS, CNPG, PgBouncer, ESO. 앱이 S3 · Secrets Manager · KMS를 직접 씀 | §7.2, T6 |
| `target-onprem` | 고객 앱 목적지 | 온프렘 장비의 Lima VM 2대(Ubuntu) | K3s(CIS 하드닝 설정), CNPG, PgBouncer, OpenBao, ESO, S3 호환 저장소(후보 Rook-Ceph RGW · RBD, 제안 D-22, 검증 중) | §7.2, T2, T9. 결정 D-21, D-23 |
| `target-gcp` | 두 번째 목적지 | GCP 서울 | GKE(Autopilot 우선). 온프렘과 같은 랜딩 존. 실제 컷오버 1회 뒤에만 세움 | 제안 D-01, 결정 D-21, D-23 |

이전 경로는 넷입니다. AWS에서 온프렘(P0, 데모), 온프렘에서 AWS로 되돌리기(P0), AWS에서 GCP(P2), 리전 이전(설계 문서에만, 제안 D-07)입니다. 자세한 것은 [docs/architecture/03-migration.md](docs/architecture/03-migration.md)에 있습니다.

## 읽는 순서

1. [docs/spec/README.md](docs/spec/README.md): 스펙과 저장소의 관계
2. [docs/architecture/00-overview.md](docs/architecture/00-overview.md): 전체 구조 한 장
3. [docs/spec/SPEC-TRACE.md](docs/spec/SPEC-TRACE.md): 요구와 작업이 어느 폴더로 가나
4. [docs/spec/TECH-STACK-SPEC.md](docs/spec/TECH-STACK-SPEC.md): 기술 스택 정본. 영역별 판과 세부는 [docs/architecture/06-tech-stack.md](docs/architecture/06-tech-stack.md)
5. [docs/plans/2026-11-03-to-07.md](docs/plans/2026-11-03-to-07.md): 일정
6. 자기 영역 폴더의 README

## 폴더

```text
bravo-platform/
├── AGENTS.md              # 사람과 코딩 에이전트 공통 작업 규칙
├── docs/
│   ├── spec/              # 스펙 사본, 추적표, 편차 제안
│   ├── architecture/      # 구조, 환경, 네트워크, 이전, 거버넌스, 리허설, 기술 스택
│   ├── adr/               # 회의에서 정한 결정
│   ├── plans/             # 일정과 영역별 계획
│   ├── runbooks/          # 컷오버, 되돌리기, 초기화, 삭제 절차
│   └── evidence/          # 실행 ID와 시각이 붙은 측정 기록
├── infra/
│   ├── modules/           # 기능 계약 + 환경별 구현(스펙 §8.8)
│   ├── envs/              # platform-aws, source-aws, target-onprem, target-gcp
│   └── cloud-init/        # 스펙 §8.8의 공통 설치 자리. K3s 노드 준비는 modules/cluster가 맡음
├── platform/              # 플랫폼(본부) 서비스
│   ├── api/               # 플랫폼 API, 상태 머신, 이벤트 스트림, 감사 로그
│   ├── collector/         # 읽기 전용 수집기
│   ├── agent/             # 에이전트 워커, 도구 계층, 의존성 인벤토리
│   ├── rehearsal/         # 리허설 러너와 Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 어댑터. 프로토타입 있음
│   ├── gate/              # 통제 시험기, 원본 대조 게이트
│   ├── governance/        # 규제 매핑표, 거버넌스 팩
│   ├── executor/          # Cutover Executor, 무결성 영수증, 되돌리기
│   └── console/           # 운영 콘솔
├── apps/demo/             # 이전 대상 데모 앱(앱 선택은 비워 둠)
├── tools/loadgen/         # 부하 생성기와 관측
├── policies/              # NetworkPolicy, Kyverno, 정책 린트 규칙
├── deploy/                # 클러스터 안 배포 매니페스트
├── tests/e2e/             # 시나리오 S1–S4 인수 시험
├── scripts/               # 저장소 검사 스크립트
└── .github/workflows/     # 빌드, OIDC 배포, 검사
```

## 명령

| 목적 | 명령 |
|---|---|
| 문서 검사 | `make check` |
| 리허설 프로토타입 시험 | `make -C platform/rehearsal check` |

클라우드 자원을 만드는 명령은 아직 없습니다. 만들 때는 팀 승인 뒤 `infra/envs/<환경>/`에서 실행합니다.
