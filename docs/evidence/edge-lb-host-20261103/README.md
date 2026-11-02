# 대표 이름 b-free-chat: LB가 Host를 덮어쓰지 않게(2026-11-03 03:56 KST)

Cloudflare LB pool이 원점 요청의 Host를 환경 이름(`b-free-chat-src` · `-dst`)으로 바꿔서, `http://b-free-chat…`의 301이 LB가 고른 환경 이름으로 갔습니다(`../edge-https-redirect-20261103/README.md` "남은 한계"). Cloudflare의 "터널 뒤 LB" 구성대로 pool 원점의 Host 덮어쓰기를 지웠습니다. 이제 평문 요청은 `https://b-free-chat.mangowhois.ai`로 돌아갑니다.

| 순서 | 일 | 기록 |
|---|---|---|
| 1 | 두 앱 터널의 ingress에 `b-free-chat.mangowhois.ai` 규칙은 이미 있었고(`infra/modules/edge/cloudflare/main.tf`), 출발지 · 온프렘 HTTPRoute `chat`의 hostnames에도 이미 있었음(`deploy/landing-zone/base/gateway.yaml`, `overlays/eks/gateway.yaml`, 03:5x 확인) | - |
| 2 | 바꾼 것: pool 원점의 `header.host` 삭제. 모니터가 Host를 따로 정하지 않으면 터널이 모르는 이름으로 검사하므로, 모니터 `header.Host`를 LB 이름으로 둠 | `infra/modules/edge/cloudflare/main.tf` |
| 3 | `keys.sh with-cloudflare -- terraform -chdir=infra/envs/edge plan`: 3개 수정(모니터 1, pool 2), 추가 · 삭제 0. pool의 `networks`는 계산값 표시뿐 | `01-plan-summary.txt`, `01-plan.txt` |
| 4 | 같은 계획 파일로 apply, 03:56:47 KST | `02-apply.txt` |
| 5 | 공개 확인(이 Mac에서 Cloudflare 경유): `https://b-free-chat…/api/config` 200(served aws), `http://b-free-chat…` 301 → `https://b-free-chat.mangowhois.ai:443/…`, `http://…/ws` 301 → 같은 이름, `wss` 업그레이드 101. 환경 이름도 그대로 200과 301 | `03-public-checks.txt` |
| 6 | 1분 뒤 pool 건강: 두 pool 모두 healthy(서울 POP 15.5 ms, 29.4 ms, 응답 200). LB 가중치는 출발지 100% 그대로 | `04-pool-health.txt` |

- 목적지로 가는 요청의 Host는 이번에 직접 보내 보지 않았습니다(LB가 출발지 100%). 목적지 pool 모니터가 같은 Host로 200을 받은 것으로 경로를 확인했습니다.
- Cilium Gateway는 `Location`에 `:443`을 붙입니다(앞 증거와 같음).
