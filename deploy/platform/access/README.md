# 데모 접근과 첫 로그인 역할

콘솔은 Cloudflare Access의 이메일 One-time PIN으로 로그인한다. 팀 리뷰 계정은 `viewer`로 홈·클러스터·관측·비용·기록을 조회한다. 운영자와 승인자의 역할은 분리한다. 이메일 확인이 끝난 Access 주체의 `(iss, sub)`로 첫 로그인 때만 멤버십을 만든다.

실제 이메일과 역할 목록은 Git에 두지 않는다. `members.json`은 배포에 사용하지 않는 형식 예시다.

| 정보 | 보관 | 사용 |
|---|---|---|
| 최초 역할 목록 | OpenBao `secret/envs/platform/access-members`, `members_json` 필드 | Kubernetes Secret `bravo-control/access-members`의 `members.json`으로 전달 |
| 콘솔 입장 허용 이메일 | Cloudflare Access 정책, 운영자 로컬의 무시된 edge tfvars | One-time PIN 발급·접근 허용 |
| 실제 로그인 주체·역할 | 플랫폼 DB principals·memberships | API 인가·감사 |

운영자가 목록을 변경할 때는 비공개 JSON을 `keys.sh env-put-json platform access-members`의 표준입력으로 전달한다. JSON 외부 봉투는 `{"members_json":"<목록 JSON 문자열>"}`이다. 파일·터미널·GitHub 댓글에 토큰을 출력하지 않는다.

```sh
deploy/platform/scripts/deliver-secrets.sh access
```

처음 이 구조로 전환할 때 Secret을 먼저 전달하고 GitOps가 API와 MCP의 Secret 볼륨 변경을 적용하게 한다. 기존 멤버십은 자동 변경하지 않는다. 다음 목록 변경은 API·MCP의 롤링 재시작 후 새 로그인부터 적용된다. Cloudflare 허용 목록과 플랫폼 역할을 둘 다 확인한다.
