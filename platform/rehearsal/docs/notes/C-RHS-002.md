---
id: C-RHS-002
kind: claim
title: task.toml 필드가 Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 0.25.0과 맞다
status: 추측
evidence: doc
uses: []
proof_uses: []
owner: 류지환
updated: 2026-11-02
---

## 주장
템플릿의 `[agent] network_mode`, `[verifier] environment_mode`, `artifacts` 필드를 Harbor 0.25.0이 읽습니다.

## 전제
- task.toml 스키마는 0.25.0 기준입니다.

## 근거
- [doc] 팀원 문서의 task.toml 예와 Harbor 문서의 network policy, separate verifier 절

## 검증 방법
- 결정론 검증: `make oracle TASK=tasks/templates/fix-secret-injection`
- 판정 기준: 마지막 줄 `reward=1`이고, 결과의 정책 기록이 no-network입니다.

## 반례 시도
- 필드 이름이 다르면 Harbor가 기본값(public)으로 돌 수 있습니다. 결과의 정책 기록을 반드시 봅니다.

## 기록
- 2026-11-02 류지환: 실행 전입니다.
- 2026-11-02 정적 확인(`make test T=tests/test_harbor.py`): Harbor 0.25.0 `TaskConfig`가 세 필드를 읽습니다. 다만 `[environment] network_mode`가 없으면 환경 기본선이 public이라, Harbor가 no-network 실행 단계와 맞지 않는다며 환경 생성을 거부했습니다(반례 시도의 위험이 실제로 나타남). Trial용 task 폴더(`harbor_task.materialize`)가 환경 기본선을 no-network로 고정하고, 템플릿의 `artifacts`는 비웁니다. `make oracle` 실행은 아직입니다.
