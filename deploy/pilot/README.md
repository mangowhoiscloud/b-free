# 파일럿

**내림(2026-11-02, 팀장 지시).** 노트북 Compose 파일럿은 더 쓰지 않습니다. 실제 대상은 출발지 EKS, 온프렘 K3s, GKE, 플랫폼 EKS입니다(`../CLOUD-E2E-CONTRACT.md` 6.1). 예외는 키 보관소 `platform-bao`입니다. 아래 절차와 수치는 이력이고, 수치는 "파일럿(노트북) 기록"입니다. 데모 각본과 reset은 `docs/demo/SCRIPT.md`, `deploy/scripts/reset-demo.sh`입니다.

스펙 P0의 한 줄기를 노트북에서 실제로 돌렸습니다. 목업은 없습니다. 계약은 `CONTRACT.md`입니다.

운영자 MCP로 계획 → 게이트 BLOCK(목적지에 심은 퇴행) → 사람이 고침 → 게이트 PASS → 승인자가 콘솔에서 승인 → 운영자가 콘솔에서 컷오버(부하 중) → 쓰기 정지 ms, 무결성 영수증, 유실 0 확인.

## 처음 한 번

1. `cp .env.example .env` 뒤 `change-me` 값을 바꿉니다.
2. `uv run --project ../../platform/api python devauth/gen.py`로 개발용 키와 토큰을 만듭니다.
3. `docker compose -p bravo-pilot --env-file .env -f compose.yaml up -d --build`

## 플랫폼 금고(OpenBao, D-26)

키와 비밀은 플랫폼 OpenBao 하나만 보관합니다. 서비스는 `platform-bao`(OpenBao 2.7.2, 운영 모드, raft, TLS, 파일 감사)이고, 포트는 `127.0.0.1:8200`에만 엽니다. 모든 명령은 `scripts/keys.sh`이고, 토큰 · 키 조각 · 비밀 값을 화면에 내지 않습니다.

1. 처음 한 번: `scripts/keys.sh tls`(시험용 CA와 서버 인증서, `bao/tls/`, Git 제외) → `docker compose -p bravo-pilot -f compose.yaml up -d platform-bao` → `scripts/keys.sh init` → `scripts/keys.sh import-cloudflare`.
2. `import-cloudflare`는 `.env`의 `CLOUDFLARE_*`를 `secret/platform/cloudflare`에 쓰고, 다시 읽어 맞는지 확인한 뒤 `.env`의 토큰 값을 비웁니다. 영역 ID를 나중에 넣으면 같은 명령을 다시 돌립니다(ID만 고침).
3. 재시작 뒤: 금고는 봉인된 채 뜹니다. `scripts/keys.sh unseal`로 풀고 `scripts/keys.sh status`로 확인합니다.
4. Cloudflare 토큰 쓰기: `scripts/keys.sh with-cloudflare -- terraform plan`. 실행마다 edge AppRole의 secret_id(60초, 1회)로 5분짜리 토큰을 받아 값을 읽고 바로 폐기합니다. 값은 그 자식 프로세스의 환경에만 있습니다.
5. 터널 토큰: `scripts/keys.sh issue-tunnel-token <env> <tunnel_id>`가 Cloudflare API에서 받아 `secret/envs/<env>/cf-tunnel`에 바로 씁니다.

**보관 위치.** `${BRAVO_KEY_CUSTODY_DIR:-~/.bravo/custody}/openbao-init.json`(파일 600, 폴더 700, 저장소 밖)입니다. 키 조각 3개(2개로 풂), 폐기한 첫 루트 토큰, 운영자 토큰 2개(`platform-admin`, `platform-edge-issuer`, 168시간 주기, 쓸 때마다 갱신)가 들어 있습니다. 이 파일 하나로 금고 전체가 열립니다. 채팅 · Git · 공유 폴더에 두지 않고, 백업은 암호화한 곳에만 둡니다.

**루트 토큰 다시 만들기.** `scripts/keys.sh generate-root`가 `platform-admin` 토큰과 키 조각 2개로 새 루트 토큰을 만들어 보관 파일에만 씁니다(`sys/generate-root-token`). 일이 끝나면 `scripts/keys.sh revoke-root`로 폐기합니다. OpenBao 2.7은 인증 없는 `sys/generate-root/*`를 기본으로 막습니다. 운영자 토큰까지 만료됐으면 `bao/bao.hcl`의 listener에 `disable_unauthed_generate_root_endpoints = false`를 잠시 넣고 `platform-bao`만 재시작해 옛 절차를 씁니다(시험하지 않음).

## 리허설 서비스(platform/rehearsal)

리허설은 언제나 실제 샌드박스가 있는 Harbor Trial입니다. 목적지 K3s가 없으면 서비스는 `cluster_unconfigured`로 거부하고 아무것도 돌리지 않습니다. 파일은 `compose.rehearsal.yaml`(서비스, 합성 DB `rehearsal-synthetic-db`, 플랫폼 DB의 증거 역할 `bravo_rehearsal_ro` 한 번 적용)이고, `compose.gate.yaml`처럼 같은 프로젝트 · 네트워크에서 따로 띄웁니다.

1. 처음 한 번(운영자, 금고가 풀린 뒤): `scripts/rehearsal-bao.sh setup` → `put-evidence-db` → `put-synthetic-db` → `printf 'host=src-db\nport=5432\ndbname=...\nuser=collector_ro\npassword=...\n' | scripts/rehearsal-bao.sh put-collector-ro pilot-src`(목적지도 같은 방식, 값은 파이프로만). 정책은 `bao/policies/platform-rehearsal.hcl`이고 위 세 경로 읽기만 엽니다.
2. 시작: `scripts/rehearsal-bao.sh with-rehearsal -- docker compose -p bravo-pilot --env-file .env -f compose.rehearsal.yaml up -d --build`. secret_id(60초, 1회)는 그 명령의 환경에만 있고, 서비스는 시작 때 로그인해 주기 토큰(1시간, 스스로 갱신)을 받습니다. 재시작은 같은 명령으로 합니다(`restart: "no"`).
3. API 연결: `.env`의 `PLATFORM_REHEARSAL_URL=http://rehearsal:8090`, `REHEARSAL_API_TOKEN`을 채운 뒤 `api`를 다시 띄웁니다. 비우면 API는 `rehearsal_unconfigured`로 거부합니다.
4. 확인: `scripts/rehearsal-bao.sh status`(값 없이 정책 · 역할 · 비밀 유무). 게이트는 `http://gate:8084`, 금고는 `https://platform-bao:8200`(`bao/tls/ca.crt`)로 붙습니다.

## 시연 직전

1. `scripts/reset_data_plane.sh` — 데이터면을 처음 상태로(심은 퇴행 포함).
2. 콘솔 `http://localhost:5173`, Grafana `http://localhost:3300`을 엽니다.
3. 되돌리기까지: `SMOKE_SCENARIO=roundtrip scripts/cutover_smoke.sh`
4. 한 번에 보기: `uv run --project ../../platform/api python scripts/pilot_e2e.py`
   - 화면으로 보이려면 1–4단계(MCP)는 Claude Code에서, 승인과 컷오버는 콘솔에서 사람이 누릅니다.

## 파일럿(노트북) 기록(2026-11-02, 부하 초당 20건, 40초)

**클라우드 결과가 아닙니다.** Compose 파일럿은 내렸습니다(`../CLOUD-E2E-CONTRACT.md` 6.1). 아래 값은 모두 실행 ID가 있는 파일에서 옮겼습니다. 원본 사본과 실행별 PASS · FAIL은 [docs/evidence/pilot-notebook/RUNS.tsv](../../docs/evidence/pilot-notebook/RUNS.tsv)에 있습니다(감사 A1, B22).

| 실행 ID(`runs/`) | 결과 | 쓰기 정지 | 영수증 | 판정 |
|---|---|---|---|---|
| `smoke-20261102T184047` | 컷오버 DONE | 101 ms | MATCH | PASS |
| `smoke-20261102T184633` | 컷오버 DONE | 105 ms | MATCH | PASS |
| `smoke-20261102T191253` | 컷오버 DONE, 되돌리기 DONE | 76 ms, 56 ms | MATCH, MATCH | FAIL. S4-AC1 목적지 314행, 출발지 599행 |
| `smoke-20261102T191406` | 컷오버 DONE, 되돌리기 DONE | 139 ms, 45 ms | MATCH, MATCH | PASS. S4-AC1 315행 같음 |
| `smoke-20261102T192514` | 컷오버 DONE, 되돌리기 DONE | 80 ms, 52 ms | MATCH, MATCH | PASS. S4-AC1 314행 같음 |
| `smoke-20261102T194436` | 컷오버 DONE | 92 ms | MATCH | FAIL. 채팅 트래픽 파일이 비었고 부하 요약이 없음 |
| `smoke-20261102T194530` | 컷오버 DONE, 되돌리기 DONE | 87 ms, 49 ms | MATCH, MATCH | PASS. S4-AC1 496행 같음, 채팅 400건 ack 400 |
| `smoke-20261102T203049` | 컷오버 DONE, 되돌리기 DONE | 94 ms, 70 ms | MATCH, MATCH | PASS. S4-AC1 495행 같음, 채팅 400건 ack 400 |
| `smoke-20261102T184416` | 불일치 주입, ABORT_PRE_WRITE | `null`(단계 합 61 ms) | MISMATCH | PASS(기대한 중단) |
| `smoke-20261102T192634` | 불일치 주입, ABORT_PRE_WRITE | `null`(단계 합 66 ms) | MISMATCH | PASS(기대한 중단) |
| `smoke-20261102T194650` | 불일치 주입, ABORT_PRE_WRITE | `null`(단계 합 82 ms) | MISMATCH | PASS(기대한 중단) |

- 컷오버 DONE의 쓰기 정지는 76, 80, 87, 92, 94, 101, 105, 139 ms(8회)입니다. 앞 판의 "18:52 107 ms", "18:58 97 ms"는 실행 파일이 없어 지웠습니다. "19:26 불일치 42 ms"는 실행 파일(`write_pause_ms: null`, 단계 합 66 ms)과 달라 고쳤습니다.
- 계약 시험 E1–E6(`scripts/contract_tests.sh`, `runs/contract-*`)은 판정 보류입니다. 파일에는 출력만 있고 시험별 판정 줄이 없습니다. 감사 B12(E5는 구조상 늘 통과), B13(RESUME 실패를 무시하고 효과 기록을 스크립트가 씀)이 고쳐진 뒤 다시 돌립니다. `contract-20261102T191626`은 e4만 있는 불완전 실행입니다.
- 한계: 승인 ID는 가짜(`smoke-approval`, 감사 A2), 첨부 객체는 대조하지 않음(B10), 부하 생성기의 중복 검사는 구조상 0(B14), DB는 CNPG가 아니라 PostgreSQL 공식 이미지입니다.
- 스펙의 부하 기준(50 req/s)으로 잰 기록은 없습니다.
