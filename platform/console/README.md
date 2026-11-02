# platform/console

운영 콘솔입니다(FR-13, T21). 상태 머신 진행, 게이트 판정과 통제 비교표, 결속된 승인, 컷오버 단계와 쓰기 정지 ms, 무결성 영수증, 이벤트 흐름, 그리고 플랫폼 전체를 실시간으로 보는 화면들을 보입니다. 실제 API와 수집기만 부릅니다. mock 모드는 없습니다.

배포 위치는 우리 플랫폼 `https://b-free.mangowhois.ai`(플랫폼 EKS, Gateway `bravo-gateway`)입니다(deploy/CLOUD-E2E-CONTRACT.md §6). 노트북 Compose 파일럿은 내렸습니다(§6.1).

## 실행

| 무엇 | 명령 |
|---|---|
| 의존성과 빌드 | `pnpm install && pnpm build` (strict TypeScript 검사 뒤 Vite 빌드) |
| 타입 검사 | `pnpm typecheck` |
| 개발 서버 | `VITE_API_BASE=<API 주소> pnpm dev` (포트 5173, 다른 포트는 `--port`). `/svc/<name>/`는 `BRAVO_COLLECTOR_UPSTREAM` · `BRAVO_GATE_UPSTREAM` · `BRAVO_PROMETHEUS_UPSTREAM`으로 넘김(기본 localhost 8083 · 8084 · 9090) |
| 이미지 | `docker build -t bravo-console .` |
| 컨테이너 | `docker run -p 8080:8080 -e BRAVO_API_BASE=<API 주소> bravo-console` (환경 변수는 아래 표) |

- 이미지에는 API 주소가 없습니다. 컨테이너가 시작할 때 `BRAVO_API_BASE`로 `/config.json`을 만듭니다. 값이 없거나 http(s)가 아니면 시작하지 않습니다.
- API는 콘솔 출처를 CORS에 넣어야 합니다(`PLATFORM_CORS_ORIGINS`).
- 기본 이미지는 digest로 고정합니다. 빌드는 `node:22-alpine`, 제공은 `nginxinc/nginx-unprivileged:1.30.6-alpine3.24`(uid 101, 읽기 전용 루트 가능)입니다.

## 화면

왼쪽 Astryx `SideNav`로 화면을 고릅니다. 주소의 해시(`#/clusters` 등)가 화면입니다. 디자인 시스템은 Astryx(`@astryxdesign/core`, `@astryxdesign/theme-neutral` 0.1.4, 피어 `@stylexjs/stylex` 0.18.3, 모두 정확한 버전 고정)입니다. `src/astryx-bravo.css`가 Astryx 토큰을 콘솔 토큰(`styles.css`)에 이어 밝게 · 어둡게를 함께 바꿉니다.

| 화면 | 출처 | 갱신 |
|---|---|---|
| 홈(첫 화면) | 로그인 사용자와 역할 `GET /v1/me`, 환경 `GET /v1/environments` + 수집기 클러스터 상태, 계획 목록 `GET /v1/plans`(상태 · 출발 → 목적 · 승인 대기, 줄을 누르면 계획 화면). 운영자는 "새 이전 시작"(`POST /v1/plans`), 승인자는 내 승인 대기 표. 경로가 없으면 "연결 안 됨"과 이유 | 5초(환경 10초) |
| 계획 · 승인 콘솔 | API `GET /v1/plans/{id}`, 계획 SSE. 기존 게이트 · 승인 · 컷오버 · 이벤트 칸. `/plans/<id>` 주소로 열면 이 화면 | SSE, 15초 |
| 통제 예외 (S1 5b) | 게이트 표의 측정된 목적지 FAIL 행에서 operator가 "예외 요청"(사유와 대체 통제 각 10–2000자, 만료 5분–30일; 판정의 증거 유효기간이 없거나 지났으면 요청 불가), approver가 계획 화면 · 승인 기록의 예외 칸에서 그 FAIL의 증거(판정 행, digest, 게이트 보고서의 현재 증거)를 보고 승인 · 거절. 승인된 행은 "예외 승인"과 승인자 · 기간 · 대체 통제로 보이고 동등 판정 요약에서 따로 셈. UNKNOWN은 예외 불가. 계약 `docs/architecture/15-r2-contracts.md` §9 | 15초, 이벤트 |
| 승인 (change · cutover · failback) | 계획 SSE `approval.granted`, 기존 승인 칸. change의 diff · 이미지 digest · 정책 diff는 API 계약 문서가 생길 때까지 "연결 안 됨" | SSE |
| 에이전트 실시간 | 계획 SSE `trial.*`, `tool.denied`, `analysis.done`. 허용된 도구 호출은 API가 내보내지 않아 "연결 안 됨" | SSE |
| 클러스터 실시간 | 수집기 `GET /v1/clusters`(읽기 전용 get/list, Secret 안 읽음): 노드, 워크로드 준비, 랜딩 존 구성 요소. 프로비저닝 상태는 상태 원장이 열릴 때까지 "연결 안 됨" | 5초 |
| CI/CD · 워크플로우 | 계획 SSE(게이트 · 승인 · 실행 단계 · 영수증), 게이트 `GET /v1/catalog`. GitHub Actions(저장소 push 보류)와 Terraform은 "연결 안 됨" | SSE, 5초 |
| 데모 앱 상태 | 환경마다 한 줄: 수집기가 본 클러스터의 ns `chat` 워크로드, 공개 이름(`BRAVO_APP_ENVS`)과 공개 DNS-over-HTTPS 확인, 열린 계획의 마지막 컷오버 영수증. 앱 지표 · PgBouncer 대상 · 쓰기 차단 스위치는 경로가 없어 "—"와 이유 | 5초, DNS 15초 |
| 관측 | Grafana · Prometheus · Tempo 링크(설정이 없으면 "연결 안 됨"), Prometheus 즉시 질의 | 5초 |
| 마무리 장면 | 수집기 `GET /v1/inventory`(남은 의존, 근거 원문은 보이지 않음). 감사 기록(T24) · 태그별 비용(T1)은 "연결 안 됨" | 10초 |

화면 규칙:

- 지어낸 값이 없습니다. 읽기에 실패하면 "연결 안 됨 · 이유 · 시도한 엔드포인트"를 한 줄로 보입니다. 값이 없는 칸은 "—"와 이유입니다.
- 위치는 데이터가 보여 주는 만큼만 씁니다. 데모 앱 위치는 수집기가 돌려준 클러스터(공급자 · ID · 컨텍스트)에서 만들고, 코드에 적지 않습니다.
- 한 구획에 표면 하나. 상자 안에 상자를 두지 않고, 숫자는 표와 설명 목록(Astryx `Table`, `MetadataList`)에 둡니다. 숫자 열은 오른쪽 정렬 · 고정 폭 숫자 · 단위를 붙입니다.
- 색은 상태에만 씁니다: 정상(초록), 준비 중(노랑), 응답 없음(빨강), 연결 안 됨(회색).

## 런타임 설정과 읽기 전용 프록시

브라우저는 플랫폼 서비스를 직접 부르지 않습니다. 콘솔 nginx가 `/svc/collector/`, `/svc/gate/`, `/svc/prometheus/`를 같은 출처로 넘깁니다. GET · HEAD만 받고(`limit_except`, 그 밖은 403), `Authorization` · 쿠키 · `Cf-Access-Jwt-Assertion`은 넘기지 않습니다. 컨테이너가 시작할 때 `docker/40-bravo-config.sh`가 아래 값으로 `/config.json`과 프록시 설정을 씁니다.

| 환경 변수 | 기본값 | 뜻 |
|---|---|---|
| `BRAVO_API_BASE` | 없음(필수) | 브라우저가 부르는 API 출처 |
| `BRAVO_COLLECTOR_UPSTREAM` · `BRAVO_GATE_UPSTREAM` · `BRAVO_PROMETHEUS_UPSTREAM` | `http://collector:8083` · `http://gate:8084` · `http://prometheus:9090` | 프록시 대상(클러스터 안 Service 이름) |
| `BRAVO_DNS_RESOLVER` | `127.0.0.11` | nginx가 프록시 대상 이름을 푸는 DNS(클러스터에서는 kube-dns 주소) |
| `BRAVO_GRAFANA_URL` · `BRAVO_PROMETHEUS_URL` · `BRAVO_TEMPO_URL` | 비어 있음 | 관측 화면 링크. 비어 있으면 "연결 안 됨" |
| `BRAVO_PUBLIC_FRONT_HOST` | `b-free-chat.mangowhois.ai` | 고객 데모 앱 대표 이름(`b-free`는 플랫폼) |
| `BRAVO_APP_ENVS` | `source-aws=b-free-chat-src.mangowhois.ai,target-onprem=b-free-chat-dst.mangowhois.ai` | 수집기 클러스터 ID(CLOUD-E2E-CONTRACT §2 환경 ID) 또는 kubectl 컨텍스트 → 공개 이름 |
| `BRAVO_DOH_URL` | `https://cloudflare-dns.com/dns-query` | 공개 이름 확인용 DNS-over-HTTPS(JSON). 비우면 확인하지 않음 |

개발 서버는 `VITE_API_BASE`(필수), `VITE_GRAFANA_URL` · `VITE_PROMETHEUS_URL` · `VITE_TEMPO_URL` · `VITE_DOH_URL` · `VITE_APP_ENVS`(선택)를 읽고, `vite.config.ts`가 `/svc/<name>/`를 `BRAVO_*_UPSTREAM`으로 넘깁니다.

## API와 맞춘 것

- 인증은 Cloudflare Access뿐입니다(감사 A3). 콘솔과 API가 같은 출처(`b-free`)이고, 모든 요청(API와 `/svc/...`)이 Access 쿠키를 보내며(`credentials: 'same-origin'`), Cloudflare가 `Cf-Access-Jwt-Assertion`을 붙입니다. 쿠키 없는 요청은 Access 로그인으로 넘겨져 브라우저에서 "Failed to fetch"가 됩니다. 로그인한 사람과 역할은 `GET /v1/me`로 받고 헤더에 보입니다. 개발용 토큰 입력과 `GET /v1/dev/tokens` 호출은 없앴습니다. 계획 열기 · 새 계획은 계획 화면 안에 있습니다.
- `GET /v1/plans`(`{plans: [{plan_id, workload, state, source{name}, target{name}, pending_approval{stage}, change, created_at}]}`), `GET /v1/environments`(`{environments: [{environment_id, name, role, cluster{status, provider, nodes}, secret_status}]}`)는 `platform/api/src/bravo_api/service.py`를 따라 읽습니다. `GET /v1/me`는 형식 계약 전이라 `email`, `role`을 방어적으로 읽습니다(`src/api/home.ts`). 없는 값은 "—"입니다.
- 화면 문구는 상태를 단정하지 않습니다. 상태는 데이터(수집기 · API)에서 만들고, 출처가 없으면 "이 콘솔은 … 출처가 없습니다"라고만 씁니다. 배포판(EKS · RKE2 · K3s · GKE)은 노드 kubelet 버전에서 읽고, 환경 사이 링크는 수집기가 보는 이름에 link가 들어간 워크로드로 보입니다.
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
| `src/live/` | 실시간 화면, 폴링 훅(`useLive`), "연결 안 됨" 한 줄 표시, 표 · 설명 목록 틀(`ScreenFrame.tsx`), SSE 이벤트 접기(`planEvents.ts`) |
| `src/index.css`, `src/astryx-bravo.css` | Astryx CSS 계층 순서와 토큰 다리 |
| `docker/` | nginx 설정, 시작 때 `config.json`과 읽기 전용 프록시 설정을 쓰는 스크립트 |
