# 공개 이름의 평문 HTTP를 원점에서 https로 돌림 (2026-11-03 03:0x KST)

DEVIATIONS D-29의 발견입니다. 공개 이름 다섯 개가 `http://`에 200을 줬습니다(02:5x 측정). Cloudflare 토큰은 영역 설정과 리다이렉트 규칙을 읽거나 바꾸지 못합니다(API 10000). 영역 전체 "Always Use HTTPS"는 우리 것이 아닌 이름도 건드립니다. 그래서 Cloudflare는 바꾸지 않고, 각 원점의 Gateway API HTTPRoute에 규칙을 더했습니다.

## 무엇을 보고 돌리나 (측정, `00-header-measurement.txt`)

- cloudflared는 Gateway에 평문 HTTP로 붙습니다. 그래서 Gateway에서 `X-Forwarded-Proto`는 늘 `http`입니다(Cilium, GKE `gke-l7-rilb` 모두 https 요청에서도 `http`). 이 헤더로 돌리면 https도 돌아 무한 반복이 됩니다.
- 방문자의 scheme은 Cloudflare의 `CF-Visitor` 헤더(`{"scheme":"http"}` · `{"scheme":"https"}`)에만 있습니다. 규칙은 이 값과 정확히 같을 때만 `RequestRedirect`(scheme https, 301)입니다.
- 같은 경로에서는 헤더 조건이 많은 규칙이 이기므로 https 요청과 헤더 없는 요청(클러스터 안 호출, 상태 검사)은 원래 규칙으로 갑니다.
- GKE `gke-l7-rilb`는 헤더 일치와 `RequestRedirect`를 받습니다(경로 상태 `Reconciled`, 적용 뒤 약 30초에 동작). 앱 쪽 대안은 필요 없었습니다.

## 바꾼 매니페스트

| 이름 | 원점 | 파일 | 적용 |
|---|---|---|---|
| `b-free-chat-dst` | 온프렘 Cilium Gateway | `deploy/landing-zone/base/gateway.yaml`(HTTPRoute `chat`) | 03:05:35 `kubectl apply`(HTTPRoute만) |
| `b-free-chat-src` | 출발지 EKS Cilium Gateway | `deploy/landing-zone/overlays/eks/gateway.yaml` | 03:05:51 |
| `b-free-chat-gke` | GKE `gke-l7-rilb` | `deploy/landing-zone/overlays/gke/manifests/40-gateway.yaml` | 03:05:56 |
| `b-free-chat`(대표, Cloudflare LB) | 위 세 원점 중 LB가 고른 곳 | 위 세 경로 | - |
| `b-free` | 플랫폼 Cilium Gateway | `deploy/platform/edge/gateway.yaml`(새 HTTPRoute `bravo-control/https-redirect`) | 03:06:43 손으로 만듦. push 뒤 Argo 앱 `edge`가 같은 객체를 맡음 |

## 결과 (`05-public-curl.txt`, 노트북에서 Cloudflare 경유)

| 이름 | `http://…/api/config` | `https://…/api/config` | `https://…/ws?room=probe` 업그레이드 | `http://…/ws` |
|---|---|---|---|---|
| `b-free-chat-src` | 301 → `https://b-free-chat-src.mangowhois.ai:443/api/config` | 200 | 101 | 301 → https |
| `b-free-chat-dst` | 301 → `https://b-free-chat-dst.mangowhois.ai:443/api/config` | 200 | 101 | 301 → https |
| `b-free-chat-gke` | 301 → `https://b-free-chat-gke.mangowhois.ai/api/config` | 200 | 101 | 301 → https |
| `b-free-chat` | 301 → `https://b-free-chat-src.mangowhois.ai:443/api/config` **(이름이 바뀜, 아래)** | 200 | 101 | 301 → `https://b-free-chat-src…` |
| `b-free` | 301 → `https://b-free.mangowhois.ai/api/config`(Cloudflare Access가 터널 앞에서 이미 돌림) | 302 → Access 로그인 | 302 → Access 로그인 | 301 → https |

- 바꾸기 전(02:5x): 위 네 앱 이름은 `http://`에 200, `b-free`는 Access가 301.
- Cilium은 `Location`에 `:443`을 붙입니다(Gateway API는 "붙이지 않는 것이 좋다"). 같은 주소라 브라우저 동작은 같습니다. GKE는 붙이지 않습니다.
- `b-free` 원점 규칙은 Access 뒤라 공개 curl로는 닿지 않습니다. 플랫폼 클러스터 안에서 Gateway Service로 `Host: b-free.mangowhois.ai`를 보내 확인했습니다(`04-platform.txt`): `CF-Visitor http`는 `/`, `/v1/…`, `/mcp`, `/grafana/`, `/ws` 모두 301 → https. `CF-Visitor https`는 원래 백엔드 답(콘솔 200, `/mcp` 401, `/grafana/` 302 로그인, `/v1/health` 404). 헤더가 없으면 200. `b-free`에는 WebSocket 끝점이 없고 `/ws`는 콘솔이 답합니다(바뀌기 전과 같은 경로).

## 남은 한계 (측정)

**대표 이름 `b-free-chat`의 평문 요청은 환경 이름으로 돌아갑니다.** Cloudflare LB 풀이 원점 요청의 Host를 환경 이름으로 덮어씁니다(`infra/modules/edge/cloudflare/main.tf`의 `header.host`, 터널 ingress가 이름으로 고르기 때문). 원점에서 LB 경유와 직접 요청을 가를 헤더가 없습니다(`X-Forwarded-Host` 등 11개 없음, `CF-Connecting-IP` · `CDN-Loop` · `CF-Ray`는 양쪽 같음). 그래서 `http://b-free-chat…`는 `https://b-free-chat-<그때 LB가 고른 환경>…`으로 갑니다. 그 방문자는 이후 LB 가중치를 따르지 않습니다. https로 오는 방문자(브라우저 기본)는 영향이 없습니다. 고치려면 Cloudflare 쪽 변경이 필요합니다: 그 이름 하나에 리다이렉트 규칙(토큰 권한 필요), 또는 터널 ingress에 `b-free-chat` 규칙을 더하고 LB 풀의 Host 덮어쓰기를 빼기. 팀장 결정 사항입니다.
