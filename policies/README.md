# policies

클러스터에 적용하는 정책입니다.

| 폴더(계획) | 내용 | 작업 |
|---|---|---|
| `rehearsal/` | 리허설 네임스페이스 템플릿: NetworkPolicy 기본 거부, ResourceQuota, LimitRange, Pod Security restricted | T3 |
| `network/` | 앱 네임스페이스 NetworkPolicy(NET-01 시험 대상) | T14 |
| `kyverno/` | 출발지와 목적지에 같이 거는 규칙 | P1 |
| `proposal-lint/` | 제안서 정책 린트 규칙(와일드카드 권한 등) | T11 |
