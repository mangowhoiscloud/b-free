# 설계 주장 노트 (blueprint)

노트 하나에 주장 하나를 씁니다. Tao의 blueprint 방식처럼 주장을 작게 쪼개고, 의존과 상태를 적습니다.

## 필드

| 필드 | 뜻 |
|---|---|
| `id` | `C-<영역>-<번호>`. 영역은 SBX(샌드박스), RHS(리허설), AGT(에이전트) |
| `kind` | claim, decision, definition |
| `status` | 미정, 추측, 검증됨, 반증됨, 폐기 |
| `evidence` | doc(문서), static(정적 확인), run(실행 결과), verifier(검증기 통과) |
| `uses` | 이 주장이 기대는 노트 |
| `proof_uses` | 검증에만 필요한 노트 |

"검증됨"은 `## 기록`에 실행 명령과 결과 경로가 있어야 합니다. `scripts/check_notes.py`가 검사합니다.

## 색인

| id | 제목 | 상태 |
|---|---|---|
| C-SBX-001 | 리허설 네임스페이스는 NetworkPolicy로 밖과 끊긴다 | 추측 |
| C-SBX-002 | 에이전트 단계는 판정 파일을 바꿀 수 없다 | 추측 |
| C-RHS-001 | Trial 1회는 라이브 데모 예산 안에 끝난다 | 미정 |
| C-RHS-002 | task.toml 필드가 Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 0.25.0과 맞다 | 추측 |
