# 파일럿

스펙 P0의 한 줄기를 노트북에서 실제로 돌립니다. 목업은 없습니다. 계약은 `CONTRACT.md`입니다.

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

## 시연 직전

1. `scripts/reset_data_plane.sh` — 데이터면을 처음 상태로(심은 퇴행 포함).
2. 콘솔 `http://localhost:5173`, Grafana `http://localhost:3300`을 엽니다.
3. 되돌리기까지: `SMOKE_SCENARIO=roundtrip scripts/cutover_smoke.sh`
4. 한 번에 보기: `uv run --project ../../platform/api python scripts/pilot_e2e.py`
   - 화면으로 보이려면 1–4단계(MCP)는 Claude Code에서, 승인과 컷오버는 콘솔에서 사람이 누릅니다.

## 2026-11-02 실측(부하 초당 20건, 40초)

| 실행 | 결과 | 쓰기 정지 | 영수증 | 유실 · 중복 |
|---|---|---|---|---|
| 18:52 | WATCH | 107 ms | MATCH | 0 · 0 |
| 18:58 | WATCH | 97 ms | MATCH | 0 · 0 |
| 19:25 컷오버 → 되돌리기 왕복 | 컷오버 DONE · 되돌리기 DONE | 80 ms · 52 ms | MATCH · MATCH | 0 · 0, 목적지에서 쓴 314건이 출발지에 모두 있음(S4-AC1) |
| 19:26 불일치 주입 | ABORT_PRE_WRITE, 원복 완료 | 42 ms | MISMATCH | 0 · 0 |

계약 시험 E1–E6(강제 종료 뒤 반복 없음, 자원 잠금 충돌, lease 만료에도 UNKNOWN 유지, 관찰 전용 쓰기 거부, 옛 epoch 거부, PgBouncer 관리 세션 미해소 시 SAFE_HOLD)은 `scripts/contract_tests.sh`로 통과했습니다.

스펙의 부하 기준(50 req/s)으로 다시 재는 일은 남아 있습니다.
