# Harbor 도입 관련

| 항목 | 값 |
|---|---|
| 등급 | 1급 |
| 제목(Notion 원문) | (참고용)Horbor 도입 관련 |
| Notion | https://app.notion.com/p/40b8bee9ada4804b8ae1e5c3ee286dba |
| 위치 | final_team_bravo / 메모·회의록 / 메모 / 문제정의 / 타겟 사용자 / 시나리오 / 배규태 - 문제정의/타겟/시나리오 (하위 페이지) |
| 작성자 | 배규태(상위 페이지 이름 기준) |
| Notion 마지막 수정 | 2026-11-01 19:45:05 KST (`2026-11-01T10:45:05.785Z`) |
| 가져온 시각 | 2026-11-02 19:21 KST, Notion MCP fetch(읽기 전용) |
| 본문 sha256 | `b4fc0399c7401bcf6f9d7fb582331b3202dfda727541004219904ff8f030402d` |
| 개인 정보 | 없음(확인함) |

원문 제목의 "Horbor"는 원문 그대로입니다. 이 문서의 Harbor는 에이전트 샌드박스 · 평가 프레임워크이며, CNCF 레지스트리 Harbor와 다릅니다. 원문 제목에 "(참고용)"이 붙어 있습니다.

이 파일은 Notion 원문을 Markdown으로 옮긴 사본입니다. 내용을 고치지 않습니다. sha256은 아래 표시 줄 다음부터 파일 끝까지의 바이트로 계산합니다.

<!-- body -->
## 1. Harbor 한 줄 요약

- 에이전트를 격리된 샌드박스에서 실행하고, 결과를 검증기로 채점하는 **오픈소스 에이전트 평가 프레임워크** (Terminal-Bench 제작팀)
- 기본 단위는 **Task** = 지시문 + 실행 환경 + 검증 스크립트. 폴더 하나로 정의

```text
my-task/
├── instruction.md   ← 에이전트에게 주는 지시
├── task.toml        ← 설정 (네트워크, 타임아웃 등)
├── environment/     ← 샌드박스 정의 (Dockerfile, docker-compose)
├── solution/        ← 정답 예시 (선택)
└── tests/test.sh    ← 검증 스크립트
```

- **Trial**(시도 1회): 환경 시작 → 에이전트 실행 → 검증기가 점수(reward) 기록 → 환경 정리

## 2. 우리 파이프라인과의 대응

| Harbor 개념 | 우리 파이프라인에서 |
| --- | --- |
| Task (지시 + 환경 + 테스트) | 마이그레이션 단계 하나 (예: S3 권한 변환) |
| Environment (샌드박스) | 대상 온프렘을 모사한 격리 환경 |
| Agent | 설정 변환·수정안을 만드는 에이전트 |
| Verifier → reward | 원본 대조 게이트 판정 (통과 1 / 차단 0) |
| Network policy | 에이전트는 LLM API만 접근, 운영망 차단 |
| Separate verifier | 에이전트가 검사 로직을 건드리지 못하게 분리 |
| Trajectory (행동 기록) | 에이전트가 한 일 전부 기록 → 감사·재현 |
| Job (여러 Trial) | 같은 작업 반복 실행 → 신뢰성 수치 |

→ "에이전트는 샌드박스에서 제안하고, 판정은 검증기가 한다" = 우리 설계 원칙("에이전트를 믿는 게 아니라 결과를 검증한다")과 같은 구조

## 3. 도입 방안

### 방안 1 (추천): 런타임 샌드박스는 직접, Harbor는 신뢰성 평가에만

- **런타임**: 이전 대상 환경을 모사한 Docker·K8s 네임스페이스를 우리가 직접 띄움. Harbor의 설계 방식(단계별 네트워크 정책, 검증기 분리, 행동 기록)만 차용
- **평가**: 결함을 심은 시나리오(시퀀스 불일치, 열린 버킷, 권한 누락 등)를 Harbor 작업으로 만들어 에이전트를 반복 실행 → "결함 N종 수정 성공률 X%, 금지 접근 시도 차단 Y건" 같은 수치를 발표 근거로 사용
- 장점: 데모 속도에 영향 없음, 위험 낮음 / 단점: 런타임 격리는 직접 구현해야 함

### 방안 2: 런타임에도 Harbor 사용 (데모 핵심 단계 1~2개만)

- 예: "S3 권한 → MinIO 권한 변환"을 Harbor 작업 하나로 구성
  - `instruction.md`: 원본 S3 정책을 MinIO 정책으로 변환해 `/out/proposal.json`에 저장
  - `environment/`: MinIO·PostgreSQL·Vault를 docker-compose로 띄운 온프렘 모사 환경. 원본 정책 스냅샷은 읽기 전용
  - `tests/test.sh`: 원본 대조 게이트 로직. 제안 정책을 적용하고 "인증 없이 접근되나?"를 원본 기대값과 비교해 1/0 기록

```toml
artifacts = ["/out/proposal.json"]   # 에이전트 결과물만 검증기로 전달

[agent]
network_mode = "allowlist"
allowed_hosts = ["api.anthropic.com"]  # LLM API만 허용

[verifier]
environment_mode = "separate"   # 검증은 별도 샌드박스에서
network_mode = "no-network"
```

- 흐름: 파이프라인이 작업 폴더 생성 → `harbor run` → reward 1이면 승인 단계, 0이면 수정안 재시도(최대 3회)
- 장점: "검증된 툴로 샌드박스 신뢰성 확보" 내러티브와 직결 / 단점: 아래 문제점

## 4. 예상 문제점

| 문제 | 내용 | 영향 | 대응 |
| --- | --- | --- | --- |
| 실행 시간 | Trial마다 환경 시작 → 에이전트 설치 → 실행 → 검증(분리 시 샌드박스 1개 추가) → 정리 | 라이브 데모에서 단계당 대기 시간 | 이미지 재사용·설치 생략 옵션, 사전 시간 측정 |
| 정적 작업 구조 | 작업 폴더가 미리 정의된 형태인데, 우리는 이전마다 입력(원본 정책, DB 스냅샷)이 다름 | 작업 폴더를 동적으로 만드는 연결 코드 필요 | 작업 템플릿 + 스냅샷 주입 |
| 운영 기능 없음 | 사람 승인·컷오버·롤백은 Harbor 범위 밖 | Harbor는 메인 엔진이 될 수 없음 | 우리 상태 머신이 담당 |
| 검증기 분리 제약 | 분리된 검증기는 복사된 결과물만 볼 수 있음(실행 중 프로세스 등 전체 상태는 못 봄) | 원본 대조에 필요한 정보를 결과물로 명시해야 함 | artifacts 경로 설계 |
| 로컬 환경 조건 | Docker 허용 목록은 리눅스 컨테이너 + nftables 필요. Daytona·Modal 등은 허용 목록이 단일 컨테이너에서만 동작 | 시연 장비에서 네트워크 정책이 안 걸릴 위험 | 시연 장비에서 사전 확인 |
| 용어 혼선 | 회의에서 말한 "리플레이"는 문서상 행동 기록 불러오기·재채점 기능에 가까움 | 발표 문구 과장 위험 | 발표 문구 맞추기 |

## 5. 다음 액션

- [ ] 로컬 Docker로 Trial 1회 시간 측정 (MinIO + PostgreSQL 환경, 권한 변환 작업) → 방안 1/2 결정
- [ ] 시연 장비에서 네트워크 허용 목록 동작 확인
- [ ] 평가용 결함 시나리오 목록 확정
- [ ] 발표 문구 정리 ("리플레이" 등)

## 참고

- [Harbor 문서](https://docs.harborframework.com/)
- [Task 구조](https://docs.harborframework.com/tasks/overview.md)
- [네트워크 정책](https://docs.harborframework.com/tasks/network-policies.md)
- [샌드박스 종류·제약](https://docs.harborframework.com/sandboxes/pre-integrated-sandboxes.md)
- [커스텀 에이전트](https://docs.harborframework.com/agents/custom-agents.md)
- [검증기 분리](https://docs.harborframework.com/news/separate-verifier-sandboxes.md)
