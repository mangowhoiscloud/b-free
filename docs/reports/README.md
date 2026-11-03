# B-free 기술 리포트

2026-11-03 기준. 팀 설계 문서의 시각화 언어로 현재 구현·원리·실측·결함·미결정을 설명합니다. 각 보고서는 독립적으로 읽을 수 있고 원본 증거와 다음 읽기를 포함합니다.

| 보고서 | 본문 | 시각화 |
|---|---|---|
| 01 · 전체 구조 — 플랫폼은 무엇을 옮기고 무엇을 증명하나 | [읽기](01-architecture.md) | [그림](figures/01-architecture.html) |
| 02 · 장애 분석 — Source Gateway 503은 왜 노드가 늘자 나타났나 | [읽기](02-gateway.md) | [그림](figures/02-gateway.html) |
| 03 · S2 — 정책 번역과 코드 수정을 어떻게 검증했나 | [읽기](03-s2.md) | [그림](figures/03-s2.html) |
| 04 · 검증기 리뷰 — 무엇이 PASS의 의미를 바꿨나 | [읽기](04-verifier.md) | [그림](figures/04-verifier.html) |
| 05 · SHIFT — 연결 풀 지연과 표본 부족은 다른 문제다 | [읽기](05-shift.md) | [그림](figures/05-shift.html) |
| 06 · 부하 측정 — 성공한 쓰기, 실패한 시도, 읽기 방식을 분리하자 | [읽기](06-load.md) | [그림](figures/06-load.html) |
| 07 · 컷오버와 복구 — 쓰기 DB를 하나만 유지하는 절차 | [읽기](07-cutover.md) | [그림](figures/07-cutover.html) |
| 08 · 데모 시간 — S3가 80초 안에 들어간다고 말할 수 없는 이유 | [읽기](08-demo-budget.md) | [그림](figures/08-demo-budget.html) |
| 09 · 운영과 접근 — 키를 공유하지 않고 팀원이 데모를 보게 하기 | [읽기](09-operations.md) | [그림](figures/09-operations.html) |
| 10 · 로드맵과 협업 — 다음 결과를 무엇으로 확인할 것인가 | [읽기](10-roadmap.md) | [그림](figures/10-roadmap.html) |

HTML 그림은 확대할 수 있으며 05와 08에는 가정을 바꾸는 계산 도구가 있습니다. 계산은 새 측정값이 아닙니다. SVG는 GitHub에서 바로 볼 수 있습니다. [시각화 기준](visual-style.md)
