# 플랫폼 게이트 두 쌍, 입장 정책을 건 뒤 (2026-11-03 01:56–01:57 KST)

`../platform-gate-20261102T165445Z/`와 같은 실행(Service `gate`, POST, 이미지 `ec47f7c3`)을 ValidatingAdmissionPolicy `bravo-gate-netprobe`를 세 클러스터에 건 뒤 다시 했습니다(`../netprobe-admission-*/`). 결과가 같습니다. UNKNOWN 0, NET-PATHS 시험 Pod는 세 클러스터 모두 만들어지고 지워졌습니다. 남은 FAIL: 온프렘 OBJ-01 · OBJ-05(심은 회귀), GKE OBJ-06, 출발지 NET-DEFAULT-DENY · NET-PATHS.
