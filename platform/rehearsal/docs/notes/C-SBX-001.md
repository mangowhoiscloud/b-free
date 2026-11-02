---
id: C-SBX-001
kind: claim
title: 리허설 네임스페이스는 NetworkPolicy로 밖과 끊긴다
status: 추측
evidence: doc
uses: []
proof_uses: []
owner: 류지환
updated: 2026-11-02
---

## 주장
리허설 네임스페이스의 Pod는 네임스페이스 밖, 메타데이터 주소, 인터넷에 닿지 못합니다.

## 전제
- 목적지 K3s(D-21)는 flannel VXLAN을 쓰고, K3s 내장 네트워크 정책 컨트롤러가 NetworkPolicy를 집행합니다.
- 기본 거부 정책과 네임스페이스 안 통신 허용 정책을 함께 둡니다. 기본 거부는 Kyverno 생성 정책이 만듭니다.

## 근거
- [doc] K3s는 기본 flannel 위에서 내장 네트워크 정책 컨트롤러로 NetworkPolicy를 집행합니다. 출처: 저장소 상위의 `docs/architecture/09-cni-csi.md` 1절

## 검증 방법
- 결정론 검증: canary Pod에서 외부 주소, 169.254.169.254, 다른 네임스페이스 Service로 접속합니다.
- 판정 기준: 세 접속이 모두 실패하고, 같은 네임스페이스 DB 접속은 성공합니다.
- 검증기 위치: `src/bravo_rehearsal/harbor_env.py`의 `isolation_check`(2026-11-02 구현. 두 네임스페이스 모두 `start()`에서 에이전트보다 먼저 돎. 실제 K3s 실행은 아직)

## 반례 시도
- Flannel 단독 구성이면 NetworkPolicy가 집행되지 않습니다. 시연 장비의 CNI부터 확인합니다.
- 정책을 바꾸는 순간 열려 있던 연결이 남는지는 CNI마다 다릅니다.

## 기록
- 2026-11-02 류지환: 문서 근거로 추측 상태로 작성했습니다.
- 2026-11-02: 목적지가 K3s로 바뀌어(D-21) 전제와 근거를 고쳤습니다. 상태는 그대로 추측입니다.
