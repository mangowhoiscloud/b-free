---
id: C-SBX-002
kind: claim
title: 에이전트 단계는 판정 파일을 바꿀 수 없다
status: 추측
evidence: doc
uses: [C-SBX-001]
proof_uses: []
owner: 류지환
updated: 2026-11-02
---

## 주장
에이전트가 리허설 환경에 reward 파일을 미리 써 두어도 판정은 바뀌지 않습니다.

## 전제
- task는 분리 검증기(`environment_mode = "separate"`)를 씁니다.
- 검증기는 시작 전에 이전 reward 파일을 지웁니다.

## 근거
- [doc] 같은 컨테이너에서 채점한 Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 벤치마크가 미리 써 둔 reward 파일로 뚫렸습니다(BenchJack, arXiv:2605.12673).
- [static] Harbor 0.25.0 검증기는 시작 전에 이전 보상 파일을 지웁니다. 출처: `research/sandbox-research/08-harbor-k8s-fit.md` 5.2절

## 검증 방법
- 결정론 검증: 에이전트 대신 `/logs/verifier/reward.txt`에 1을 쓰는 가짜 에이전트로 Trial을 돌립니다.
- 판정 기준: 판정이 FAIL이거나 reward 0입니다.

## 반례 시도
- 산출물 안에 검증기가 실행하는 스크립트가 섞여 있으면 판정이 오염됩니다. 산출물은 신뢰하지 않는 입력으로 다룹니다.

## 기록
- 2026-11-02 류지환: 논문과 정적 분석 근거로 작성했습니다. 0.25.0이 이 공격을 막는지 잰 공개 결과는 없습니다.
