# tools/loadgen

채팅 쓰기 부하 생성기입니다(T18, NFR-2). 지금 있는 것만 적습니다.

- 쓰기만 합니다. 읽기 경로는 없습니다. 기본 초당 20건(`--rate`, `LOADGEN_RATE`), 동시 8(`--concurrency`)입니다. 스펙 §5.3 제안값(쓰기 50, 읽기 150 req/s)으로는 아직 재지 않았습니다.
- 대상은 PgBouncer를 거친 `app_writer` 연결(`--dsn`, `LOADGEN_DSN`)입니다. 요청마다 요청 ID(= `idem_key`)와 시도 ID가 있고, 연결 오류 · 정지 중 시간 초과는 같은 `idem_key`로 다시 보냅니다(`ON CONFLICT DO NOTHING`).
- 끝나면 JSON 한 줄을 냅니다: 보냄, 확인, 재시도, 실패, 지연(최대 · p50 · p99), 가장 긴 확인 공백.
- `--verify`: 최종 DB에서 확인한 요청마다 본문 표지(`load message <요청 ID>`)를 가진 행 수를 셉니다. 0이면 유실, 2 이상이면 중복입니다(UNIQUE `idem_key`만 보면 중복이 구조상 0이라서 본문으로 셈). `ok`는 확인 > 0, 보냄 = 확인 + 실패, 유실 0, 중복 0, 실패 0일 때만 참입니다.

```bash
uv run python -m bravo_loadgen --dsn "$LOADGEN_DSN" --rate 20 --duration 60 --verify
```
