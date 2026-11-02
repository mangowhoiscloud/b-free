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
| `docker/` | nginx 설정, 시작 때 `config.json`을 쓰는 스크립트 |
