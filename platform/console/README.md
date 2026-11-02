# platform/console

운영 콘솔입니다(FR-13, T21). 한 화면에 상태 머신 진행, 게이트 판정과 통제 비교표, 결속된 승인, 컷오버 단계와 쓰기 정지 ms, 무결성 영수증, 이벤트 흐름을 보입니다. 실제 API만 부릅니다. mock 모드는 없습니다.

## 실행

| 무엇 | 명령 |
|---|---|
| 의존성과 빌드 | `pnpm install && pnpm build` (strict TypeScript 검사 뒤 Vite 빌드) |
| 개발 서버 | `VITE_API_BASE=<API 주소> pnpm dev` (포트 5173) |
| 이미지 | `docker build -t bravo-console:pilot .` |
| 컨테이너 | `docker run -p 5173:8080 -e BRAVO_API_BASE=<API 주소> bravo-console:pilot` |
| 파일럿 묶음 | `deploy/pilot/compose.yaml`이 `compose.console.yaml`을 포함합니다. `.env`에 `CONSOLE_HOST_PORT`, `CONSOLE_API_BASE`가 있어야 합니다 |

- 이미지에는 API 주소가 없습니다. 컨테이너가 시작할 때 `BRAVO_API_BASE`로 `/config.json`을 만듭니다. 값이 없거나 http(s)가 아니면 시작하지 않습니다.
- API는 콘솔 출처를 CORS에 넣어야 합니다(`PLATFORM_CORS_ORIGINS`).
- 기본 이미지는 digest로 고정합니다. 빌드는 `node:22-alpine`, 제공은 `nginxinc/nginx-unprivileged:1.27-alpine`(uid 101, 읽기 전용 루트 가능)입니다.

## 실시간 화면(11/2 추가)

왼쪽 Astryx `SideNav`로 화면을 고릅니다. 주소의 해시(`#/clusters` 등)가 화면입니다. 디자인 시스템은 Astryx(`@astryxdesign/core`, `@astryxdesign/theme-neutral` 0.1.4)이고, `src/astryx-bravo.css`가 Astryx 토큰을 콘솔 토큰(`styles.css`)에 이어 밝게 · 어둡게를 함께 바꿉니다.

| 화면 | 출처 | 갱신 |
|---|---|---|
| 승인 (change · cutover · failback) | 계획 SSE `approval.granted`, 기존 승인 칸. change의 diff · digest · 정책 diff는 API 계약 전까지 "연결 안 됨" | SSE |
| 에이전트 실시간 | 계획 SSE `trial.*`, `tool.denied`, `analysis.done` | SSE |
| 클러스터 실시간 | 수집기 `GET /v1/clusters`(읽기 전용 get/list, Secret 안 읽음), 플랫폼 서비스 상태 확인 | 4–5초 |
| CI/CD · 워크플로우 | 계획 SSE(게이트 · 승인 · 실행 · 영수증), 게이트 `GET /v1/catalog`. GitHub Actions와 Terraform은 "연결 안 됨" | SSE, 5초 |
| 데모 앱 상태 | 앱 `healthz` · `api/status`, Prometheus `chat_writes_total`, Executor `GET /v1/readiness`, 공개 DNS-over-HTTPS | 3초, DNS 15초 |
| 관측 | Grafana · Prometheus · Tempo 링크, Prometheus 즉시 질의 | 5초 |
| 마무리 장면 | 수집기 `GET /v1/inventory`(남은 의존). 감사 기록 · 비용은 "연결 안 됨" | 10초 |

- 브라우저는 다른 서비스를 직접 부르지 않습니다. 콘솔 nginx가 `/svc/<name>/`(collector, gate, executor, prometheus, app-src, app-dst)를 같은 출처로 넘깁니다. GET · HEAD만 받고(`limit_except`), 인증 헤더와 쿠키는 넘기지 않습니다. 시작할 때 `docker/40-bravo-config.sh`가 `BRAVO_*_UPSTREAM`(기본값은 Compose 서비스 이름)으로 설정을 씁니다. 개발 서버는 `vite.config.ts`가 같은 경로를 localhost 포트로 넘깁니다.
- `config.json`에는 `grafanaUrl`, `prometheusUrl`, `tempoUrl`, `publicFrontHost`, `dohUrl`이 더 들어갑니다(`BRAVO_GRAFANA_URL` 등, 기본값은 localhost).
- 읽기에 실패하면 지어낸 값 없이 "연결 안 됨 · 이유 · 시도한 엔드포인트"를 보입니다.

## API와 맞춘 것

- 인증은 `Cf-Access-Jwt-Assertion` 헤더입니다. 개발용 사용자는 `GET /v1/dev/tokens`에서 받습니다. 토큰은 저장하지 않습니다.
- 쓰기 요청마다 `Idempotency-Key`를 보냅니다. 승인은 승인 요청 ID마다, 컷오버는 승인 ID마다 키 하나를 씁니다. 같은 키로 다시 보내면 서버가 처음 결과를 돌려줍니다.
- 이벤트 흐름은 `fetch` 기반 SSE(`@microsoft/fetch-event-source`)입니다. `EventSource`는 헤더를 못 보내기 때문입니다. 끊기면 `Last-Event-ID`로 다시 붙습니다.
- 주소 `/plans/<plan_id>`는 API의 `console_url`과 같습니다. `#approve`로 열면 승인 칸으로 갑니다.
- 응답 형식은 계약에 아직 없는 부분이 있어 `platform/api/src/bravo_api`(11/2 기준)를 따랐습니다. 빠진 값은 지어내지 않고 "—", "값 없음"으로 보입니다.

## 파일

| 경로 | 내용 |
|---|---|
| `src/api/types.ts` | 계약의 상태, 이벤트, 실행 형식 |
| `src/api/normalize.ts` | JSON을 타입으로 바꾸는 방어적 파서 |
| `src/api/client.ts` | REST와 SSE 클라이언트, 이유 코드의 한국어 문구 |
| `src/api/derive.ts` | 계획 스냅숏과 이벤트로 화면 상태를 만듦 |
| `src/components/` | 진행 막대, 게이트, 승인, 컷오버, 이벤트 칸 |
| `src/live/` | 실시간 화면, 폴링 훅(`useLive`), "연결 안 됨" 표시, SSE 이벤트 접기 |
| `src/index.css`, `src/astryx-bravo.css` | Astryx CSS 계층 순서와 토큰 다리 |
| `docker/` | nginx 설정, 시작 때 `config.json`을 쓰는 스크립트 |
