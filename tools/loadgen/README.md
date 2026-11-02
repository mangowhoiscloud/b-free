# tools/loadgen

채팅 부하 생성기입니다(T18, NFR-1 · NFR-2, 스펙 §5.3). 두 방식이 있습니다.

| 방식 | 대상 | 쓰기 | 읽기 |
|---|---|---|---|
| 앱(`--app-url`) | 고객 앱 공개 주소(예: `https://b-free-chat-src.mangowhois.ai`, LB `https://b-free-chat.mangowhois.ai`) | `POST /api/rooms/<방>/messages` | 기본 `GET /api/rooms/<방>/messages?after=<id>`(앱 0.3.3부터). `--read-op ws`면 `/ws?room=<방>&after=<id>` 접속 → `hello` → 이력 → 닫기 |
| DB(`--dsn`) | PgBouncer(`app_writer`) | `INSERT ... ON CONFLICT (idem_key)` | 없음 |

스펙 수치(쓰기 50, 읽기 150 req/s)는 앱 방식으로 잽니다. DB 방식은 예전 T18 경로이고 앱을 거치지 않습니다.

## 앱 방식이 하는 일

- 쓰기: 요청 ID가 `Idempotency-Key`이고, 본문은 `load message <요청 ID>`입니다. 시도마다 시도 ID(`X-Bravo-Attempt-Id`)를 붙이고 실패한 시도는 `events.jsonl`에 남깁니다. 503(쓰기 차단 · 대기, `Retry-After`를 따름), 그 밖의 5xx, 429, 시간 초과, 연결 오류는 같은 키로 다시 보냅니다. 200은 이미 commit된 키의 재응답입니다. 그 밖의 응답은 실패로 끝냅니다. 시도 시간 제한 15초(§5.3), 요청 기한 120초, 최대 40회.
- 읽기: 기본은 앱 0.3.3의 HTTP 이력 읽기 `GET /api/rooms/<방>/messages?after=<마지막 id>`입니다. WebSocket 이력(`/ws?after=`)과 같은 행을 돌려주고, keep-alive라 Cloudflare · cloudflared · Envoy가 연결을 다시 씁니다. 0.3.2까지는 WebSocket뿐이었고, 읽기마다 새 연결이 생겨 출발지 노드를 포화시켰습니다. WebSocket 길은 `--read-op ws`(Job은 `READ_OP=ws`)로 따로 잽니다. `after`는 가장 최근 확인된 쓰기 ID보다 20 작게 잡아(`--read-window`) 읽기마다 최근 행 20개 안팎을 읽습니다. 읽기는 다시 시도하지 않습니다.
- 열린 부하(open loop)입니다. 응답이 늦어도 정해진 시각에 요청을 냅니다. 쓰기 정지는 처리량 감소가 아니라 지연과 재시도로 드러납니다.
- 방은 실행마다 새로 씁니다(`load-<실행 ID 앞 8자>`). 그 방의 모든 행이 이 실행의 것이라 NFR-2가 "보내지 않은 행"까지 셉니다.
- 어느 환경이 답했는지는 앱이 알려 줍니다. HTTP는 응답 헤더 `X-Served-By`, WebSocket은 `hello.env.id`입니다(`aws`, `onprem`, `gcp`). 앱 응답이 없으면(엣지 530 · 502, 시간 초과) `unknown`입니다.
- 한 프로세스는 코어 하나에 묶입니다. 읽기마다 TLS 접속이 새로 생기므로 `--workers`로 프로세스를 나눕니다(스크립트 기본 8). 일정은 프로세스 사이에 엇갈려 합쳐도 고르게 나갑니다. 결과의 `client`에 부하 생성기 자신의 CPU, 일정 지연, 이벤트 루프 지연을 적습니다. 부하 생성기가 포화된 실행은 앱 수치로 쓰지 않습니다.
- 프로세스마다 열린 요청은 200개까지입니다(`--max-inflight`). 넘으면 그 시각의 요청을 보내지 않고 `client_saturated` 실패로 셉니다. 대상이 무너졌을 때 부하 생성기가 소켓 수천 개를 쥐거나(11/3 02:44 운영자 Mac 약 9,400개) 메모리 한도에 걸려 죽지(03:09 Job, 22초 만에 2 GiB OOM) 않게 하는 장치입니다. 정상 실행에서는 수십 개면 됩니다.

## 어디서 돌리나

부하는 플랫폼 클러스터(`bravo-platform-rke2`, ns `bravo-control`)의 Job으로만 돌립니다(`k8s/job.yaml`, `deploy/cloud/load_lib.sh`). 운영자 Mac에서는 돌리지 않습니다. 데모 날 그 Mac이 kubectl, Lima VM, 브라우저를 함께 돌리는데, 11/3 02:44 부하가 소켓을 다 써서 kubectl과 curl이 `can't assign requested address`로 실패했습니다.

- Job도 공개 이름과 Cloudflare를 거쳐 사용자처럼 앱에 닿습니다. 플랫폼 노드(ap-northeast-2)에서는 Cloudflare 서울 지점으로 들어갑니다. 운영자 Mac(한국 ISP)에서는 `mangowhois.ai`가 LAX 지점으로 들어가 왕복이 약 130 ms 더 깁니다(02:2x 측정, `/cdn-cgi/trace`의 `colo`).
- 이미지는 `tools/loadgen/Dockerfile`을 buildx(linux/arm64)로 빌드해 플랫폼 ECR `bravo-platform/loadgen`(태그 덮어쓰기 금지)에 올리고 `k8s/job.yaml`에 digest로 고정합니다. 저장소는 `infra/envs/platform-aws`의 `platform_images`에 `loadgen`을 더해 만들었습니다(11/3 03:0x 적용).
- 한도: CPU 2코어, 메모리 3 GiB. 플랫폼 노드는 4코어이고 다른 플랫폼 서비스와 나눠 씁니다.
- 스크립트는 Job을 만들고, 끝나면 `/out`의 파일을 `kubectl exec cat`으로 가져오고, Job을 지웁니다. 이 Mac에서 하는 일은 NFR-2 대조(행 수 비교)뿐입니다.

개발 중 짧게 확인할 때만 로컬에서 낮은 비율로 돌립니다.

```bash
uv sync
uv run python -m bravo_loadgen --app-url https://b-free-chat-src.mangowhois.ai \
  --write-rate 2 --read-rate 4 --duration 10 --requests-file requests.jsonl > summary.json 2> events.jsonl
```

실행 스크립트(증거 폴더, 노드 CPU 표본, 지표 전송, NFR-2까지 한 번에):

| 스크립트 | 하는 일 |
|---|---|
| `deploy/cloud/load_run.sh [label]` | 컷오버 없이 일정 시간 부하(플랫폼 Job). 결과는 `docs/evidence/load/<KST>-<label>/` |
| `deploy/cloud/load_lib.sh` | Job 시작 · 멈춤(SIGTERM) · 파일 수거 · 삭제. 아래 스크립트가 source |
| `deploy/cloud/load_cutover.sh <plan-id> [label]` | 부하를 건 채 콘솔에서 사람이 시작한 SHIFT · CUTOVER · FAILBACK을 기다려 기록. 승인을 만들거나 실행을 시작하지 않음 |
| `deploy/cloud/load_nfr2.sh <run-dir> src\|dst` | executor-0 안에서 `executor_observe`로 그 방의 행을 읽어 요청 기록과 대조 |
| `deploy/cloud/load_sample.sh <context> <out>` | kubelet summary API로 노드 · Pod CPU 표본(metrics-server 없음) |
| `python -m bravo_loadgen.cutover_stats <run.json>...` | 5회 컷오버의 쓰기 정지 원측정 · 중앙값 · 최댓값, NFR-2 합계 |

## 출력

| 파일 | 내용 |
|---|---|
| `summary.json`(표준 출력 한 줄) | 쓰기 · 읽기 각각 보냄, 성공, 재응답, 실패, 시도 수, 오류 코드별 시도 수, 달성률(초당), 지연 p50 · p95 · p99 · 최대(예정 시각부터 성공까지), 가장 긴 성공 공백, 환경별 성공 수, `client` |
| `timeline.jsonl` | 1초마다 `write_ok`, `write_err_attempts`, `write_p95_ms`, `read_ok`, `read_err`, `read_p95_ms`, `served_by`(`w:aws` 같은 꼴) |
| `requests.jsonl` | 쓰기 요청마다 `request_id`, `outcome`(acked · failed), `attempts`, `id`, `served_by`, `replay`, `latency_ms`, `last_error` |
| `events.jsonl`(표준 오류) | 실패한 시도(`attempt_id`, 코드, 환경), 재시도 뒤 확인, 실패한 요청 |
| `nfr2-<side>.json` | `lost`, `duplicated`(본문 표지로 셈), `failed_requests`, `failed_but_committed`, `rows_not_sent_by_run`. 모두 0이고 확인 > 0일 때만 `ok` |

## 지표 계약(SHIFT 단계 판정이 읽는 것)

커밋 `9253b8b`(11/3 02:49) 기준으로 Executor의 SHIFT 판정은 자기가 프런트로 보내는 쓰기(초당 60, 방 `shift-probe`)를 씁니다. 이 지표는 그와 별개인 사용자 부하 쪽 관측입니다. 판정에 더하거나 대조할 때 아래 계약을 씁니다. 두 부하는 출발지에서 합쳐진다는 점에 주의합니다(아래 "측정 결과"의 용량 참고).

부하 생성기는 실행 중 지표를 OTLP/HTTP로 플랫폼 OTel Collector(`otel-collector.bravo-observe.svc:4318`)에 보내고, Collector가 플랫폼 Prometheus(`prometheus.bravo-observe.svc:9090`)에 씁니다. Job은 같은 클러스터라 Collector에 바로 닿습니다(`bravo-control`은 Collector 수신 허용 목록에 있음). 2026-11-03 02:3x KST에 플랫폼 Prometheus에서 아래 이름으로 들어온 것을 확인했습니다.

| 시계열 | 라벨 | 뜻 |
|---|---|---|
| `bravo_loadgen_attempts_total` | `run_id`, `op`(write · read · read_ws), `result`(ok · error), `code`, `served_by` | 쓰기 시도와 읽기 하나하나. `read`는 HTTP 읽기, `read_ws`는 WebSocket 읽기 |
| `bravo_loadgen_attempt_duration_milliseconds_{bucket,count,sum}` | `run_id`, `op`, `result`, `served_by`, `le` | 시도 하나의 시간(HTTP 요청 하나, WebSocket 이력 읽기 하나) |
| `bravo_loadgen_request_duration_milliseconds_{bucket,count,sum}` | `run_id`, `op`, `outcome`(쓰기 acked · failed, 읽기 ok · failed), `served_by`, `le` | 논리 요청 하나, 예정 시각부터 끝까지(재시도 포함) |

- 공통 라벨: `job="bravo-loadgen"`, `service_name="bravo-loadgen"`, `instance="<run_id>/w<번호>"`(프로세스마다 하나. 실행 단위는 `run_id`로 묶어 합칩니다), `bravo_plan_id`(`--plan-id`를 준 경우. `load_cutover.sh`는 항상 줌), `bravo_env="platform"`(Collector가 붙임).
- `code`: 쓰기 성공 `201`(새로 씀) · `200`(재응답). 쓰기 오류 `503:writes_blocked`, `503:write_timeout`, `503:starting`, `500:injected_fault`, `500`, `502`, `530`, `timeout`, `transport:<예외>`, `bad_body`, `409:idempotency_conflict`(재시도 안 함), `client_saturated`(보내지 않음). 읽기 성공 `ok`. HTTP 읽기 오류 `<상태>`(예: `503:db_waiting`, `502`), `timeout`, `transport:<예외>`, `bad_body`, `rows_outside_request`. WebSocket 읽기 오류 `http_<상태>`(핸드셰이크 거절), `close_<코드>`(예: 1013), `handshake:<예외>`, `bad_frame`.
- 히스토그램 경계(ms): 25, 50, 100, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 15000, 30000.
- 누적(cumulative) 값이고 5초마다 보냅니다(`--metrics-interval`). 15초보다 짧은 단계 창은 가르지 못합니다.

단계 `[t1, t2]`를 판정하는 쿼리(Executor가 시각을 압니다). 개수는 `increase()`의 보간 대신 두 시각의 순간값 차이를 권합니다. t1에 없던 시계열은 0으로 봅니다.

```promql
# 개수: time=t2 값 - time=t1 값
sum by (op, result, served_by) (bravo_loadgen_attempts_total{bravo_plan_id="$PLAN"})
# 오류율(목적지만): 위 차이에서 served_by="onprem"의 error / (ok + error). served_by="unknown"인 오류는 어느 쪽에서 났는지 모르므로 단계 전체의 오류로 셉니다.
# p95(목적지 쓰기 성공 시도), time=t2, 창 W = t2 - t1
histogram_quantile(0.95, sum by (le) (increase(
  bravo_loadgen_attempt_duration_milliseconds_bucket{bravo_plan_id="$PLAN", op="write", result="ok", served_by="onprem"}[$W])))
# 부하가 실제로 돌고 있는지: 마지막 표본의 나이(초). 15초를 넘거나 결과가 비면(5분 안에 표본 없음) 판정하지 않고 UNKNOWN
time() - max(timestamp(bravo_loadgen_attempts_total{bravo_plan_id="$PLAN"}))
```

2026-11-03 03:0x KST에 위 쿼리를 플랫폼 Prometheus에 실제로 던져 확인했습니다(실행 `08c2231f-…`, `bravo_plan_id` 라벨은 시험용 plan ID로 확인).

- 최소 표본: 초당 50 + 150에서 10% 단계의 목적지 몫은 초당 쓰기 5 · 읽기 15 안팎입니다. 표본 수 규칙은 판정 쪽이 정합니다.
- 서버 쪽 값도 있습니다. 앱의 SERVER span에서 만든 `traces_span_metrics_calls_total` · `traces_span_metrics_duration_milliseconds_bucket`(`service_name="bravo-demo"`, `bravo_env`, `url_path`, `http_response_status_code`)입니다. `url_path`에 방 이름이 들어가므로 `url_path=~"/api/rooms/[^/]+/messages"`로 고릅니다. HTTP 읽기는 `GET /api/rooms/<방>/messages` span이 있고, WebSocket 읽기는 span이 없습니다.
- 네트워크: 플랫폼 Prometheus의 NetworkPolicy(`deploy/platform/observability/networkpolicy.yaml`의 `prometheus-ingress`)는 2026-11-03 현재 Collector · Grafana · 콘솔만 받습니다. Executor가 읽으려면 그 규칙과 Executor 쪽 송신 규칙을 더해야 합니다.

## 측정 결과

`docs/evidence/load/README.md`에 있습니다.
