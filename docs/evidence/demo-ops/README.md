# 데모 운영 준비(리뷰 24 L4: B16–B20, P6, P9, P23)

2026-11-03 02:38–03:02 KST에 운영 Mac에서 잰 기록입니다. 대상은 `bravo-source`, `bravo-onprem`, `bravo-target-gcp`, `bravo-platform-rke2`입니다. 비밀 값은 어느 파일에도 없습니다(이름, ID, 만료 시각, 8자리 해시 지문만).

| 폴더 | 무엇 | 명령 |
|---|---|---|
| `preflight-20261102T175807Z/` | 사전 점검 1회(13초). 다른 갈래의 LB 전환 시험 도중이라 가중치 dst 0.9 · src 0.1, 대표 이름 `env.id=onprem`으로 FAIL을 잡음 | `deploy/scripts/preflight.sh --out …` |
| `preflight-20261102T180122Z/` | 사전 점검 1회(7초). PASS 98 · WARN 8 · FAIL 10. 아래 "03:01 결과" | 같음 |
| `rotation-20261102T175318Z/` | 회전 연습: 회전 전후 만료 표, 실제 회전 로그, `run` dry-run 2개 | `deploy/scripts/rotate-credentials.sh` |
| `preflight-20261102T190929Z/` | 사전 점검 1회(15초), Argo가 `3a6d168`에 동기화된 뒤. PASS 116 · WARN 7 · FAIL 1. 아래 "04:09 추가 점검" | 같음 |
| `reset-20261102T174859Z/` | reset dry-run · 실제 실행 시간, 계획 범위 삭제 시험 | `deploy/scripts/reset-demo.sh` |

## 03:01 사전 점검 결과(`preflight-20261102T180122Z/TABLE.md`)

- FAIL 10: Argo CD 앱 9줄과 `main revision`. 팀장이 main에 push(`cfe890e2`)한 직후라 배포 revision이 `75af013b`였고, 그 사이에 `deploy/platform` 변경이 있음. Argo가 따라오면 풀림. 나머지 1줄은 `operator 신원(@naver.com)`: 플랫폼 DB에 naver 신원이 없음(첫 로그인 전, P9).
- WARN 8: Cilium Helm 인증서 Secret 차이, `control`의 prune 대상 고아 ConfigMap 6개, LB 가중치(다른 갈래 시험 중), 두 세션 쿠키 없음, Mac 절전, 동결 태그 없음, `bravo-control/rehearsal-preview-s2full`(리허설 갈래 미리 보기 Pod, Ready 아님).
- PASS: 네 클러스터, OpenBao 열림, 자격 증명 35줄 모두 PASS(만료가 11/9 00:00 KST 뒤), D-16 링크 20개 실제 왕복, 소비자 3곳(수집기 세 API, Executor 두 DB, GKE ESO), 정방향 복제 `latest_end_time` 17초, RGW 암호화 객체 실제 읽기(382 ms), 터널 7개, 공개 이름 5개, Lima VM 2대.

## 회전 연습(B16 · B17)

규칙대로면 실제 회전 대상은 "11/9 06:00 KST 전에 끝나는 것"인데, 02:53 측정에서 해당이 없었습니다(`rotation-…/before.txt`). 가장 이른 것은 링크 토큰 8개 11/9 20:50 KST입니다. 그래서 경로마다 한 번씩 실제로 돌려 연습했습니다(`rotation-…/rotate.log`).

| 종류 | 대상 | 결과 |
|---|---|---|
| custody | 주기 토큰 5개 renew-self | 모두 11/10 02:53 KST로 연장 |
| k8s gate | `k8s-gate`(gate-credentials.sh onprem_token) | 새 토큰 SelfSubjectReview `kube-system:bravo-gate`, 전달본 같음, 만료 11/10 02:53 |
| k8s onprem | `collector-k8s` · `k8s-executor` · `k8s-rehearsal`(onprem-tokens.sh k8s) + Executor 파일 Secret | 세 토큰 SelfSubjectReview 확인, 전달본 같음. 회전 전 Executor 전달본은 OpenBao와 달랐음(11/9 21:46 만료 옛 토큰, preflight WARN) → 회전으로 맞춤. executor-0 안 마운트 토큰 지문 = OpenBao 지문, 링크 `dst-k8s` 경유 SelfSubjectReview `chat:bravo-executor` |
| service-token | `platform/cf-link-dst-s3http`(게이트 OBJ-06 전용 링크) | 1회차: 회전 직후 재시작한 클라이언트가 약 15초 `websocket: bad handshake`(새 비밀이 Cloudflare에 퍼지는 시간), 첫 확인 FAIL, 40초 뒤 재확인 OK. 스크립트를 고침: 회전 → Secret 전달 → 30초 대기 → 재시작 → 60초까지 재시도. 2회차: 링크 로그 ERR 0, 재시작 뒤 첫 시도 HTTP 200, 39초. 만료 11/10 02:55 |
| rgw-kms | 실행 안 함 | RGW 재시작으로 첨부가 약 40초 멈춤. 최대 수명 끝 11/10 00:41 KST. 무대 밖에서 `--kinds rgw-kms` |

Cloudflare API 토큰(플랫폼이 LB 가중치를 바꿀 때 씀)은 11/9 23:59 KST에 끝나고 스크립트로 회전할 수 없습니다(사람이 대시보드에서).

**TTL · 갱신 경보.** 플랫폼 Prometheus의 지표 이름 66개를 봤고(02:4x), 토큰 TTL · CronJob · OpenBao 토큰 지표는 없었습니다(`httpcheck_*`, 앱 · loadgen · Hubble · OTel 지표뿐, 규칙 0개, Alertmanager 없음). 그래서 경보는 preflight의 `cred` 그룹(마감 전 만료 WARN, 2시간 안 FAIL, RGW 갱신 20분 넘으면 WARN)으로 두었습니다.

## reset(B19)

| 실행 | 결과 | 시간 |
|---|---|---|
| dry-run, 계획 없음(02:48) | ALREADY 6, INFO 4(계획 없으면 지울 것을 정하지 않음, 열린 승인 0건) | 22초 |
| dry-run, `--plan` 임의 UUID(02:48:59) | ALREADY 10 | 29초 |
| 실제 실행, 같은 계획(02:49:28) | ALREADY 10, 바꾼 것 없음 | 9초 |
| 범위 시험(02:49:47): 온프렘에 `rehearsal-277d2eb5-1` · `verify-277d2eb5-1`(그 계획)과 `rehearsal-0ther000-1`(다른 계획)을 만들고 dry-run → 실제 실행 | 그 계획 둘만 PLAN → DONE(삭제), 다른 것은 OTHER로 보이기만. 다른 것은 시험 뒤 손으로 지움 | 1초, 6초 |

데모 뒤 실제 상태(컷오버 · 되돌리기 뒤)에서 reset을 잰 적은 없습니다. 컷오버 뒤 상태면 reset은 front · data · writes를 FAIL로 멈추고 콘솔의 사람 되돌리기를 기다립니다(D-18).

## 같이 찾은 것

- **노트북 loadgen이 포트를 채움.** 02:44 KST에 이 Mac의 loadgen(다른 갈래)이 Cloudflare 두 주소로 TIME_WAIT 약 9,400개를 남겼고, kubectl(EKS API)과 curl(`b-free-chat-src`)이 `connect: can't assign requested address`로 실패했습니다. 운영 Mac에서 부하를 돌리면 kubectl · 콘솔이 같이 멈춥니다. preflight `Mac TCP 포트` 줄을 더했습니다.
- **역방향 슬롯이 WAL을 쌓음.** 온프렘 chat-db의 비활성 슬롯 `bravo_rev_sub` 보존 WAL이 02:38 448 MB → 03:01 530 MB(부하 시험 중 약 3.6 MB/분). 한도 `max_slot_wal_keep_size` 2GB를 넘으면 슬롯이 사라져 S4 되돌리기 복제가 안 됩니다. 이 속도면 약 7시간입니다. 데모 전 reset의 `repl_setup rearm`이 슬롯을 다시 만듭니다. preflight는 남은 여유 512MB 아래에서 WARN, `lost`면 FAIL.
- **Argo `control` OutOfSync의 정체.** 6개 모두 prune 대상 고아 ConfigMap(kustomize 해시 이름, `prune: false`)이고 배포 차이가 아닙니다. 정리는 `argocd app sync control --prune`(쓰기, 팀장 판단). Cilium의 3개는 Helm이 비교 때마다 새로 만드는 인증서 Secret입니다.
- **동결 시험.** `demo-freeze.sh create --name demo-freeze-test-0303`(02:43, 8.6초) → `check`(6초) PASS. 시험 태그와 기록은 지웠습니다(진짜 동결은 데모 전날).

## 04:09 추가 점검(코디네이터 요청: D-30, 역방향 슬롯, PgBouncer 지속 대상, 게이트 메모리, 앱 0.3.3)

`preflight-20261102T190929Z/TABLE.md`. 남은 FAIL은 naver 운영자 신원 하나(첫 로그인 전)입니다.

| 그룹 · 줄 | 측정 | 판정 기준 |
|---|---|---|
| `path` 링크 왕복 | 온프렘 앱 Pod에서 앱 DSN(목적지 PgBouncer → `src-db-link` → 출발지 DB)으로 `SELECT 1` 15번: p50 281.8 ms, 최대 569.3 ms. 이 망의 Cloudflare POP PDX(04:08 측정은 DFW) | 쓰기 한 건 ≈ 링크 2왕복(쓰기 p50 585 ms ≈ 왕복 282 ms × 2)이라 쓰기 기준의 절반: WARN 500 · FAIL 750 ms |
| `path` 목적지 쓰기 p95 | executor-0에서 `b-free-chat-dst`로 새 연결 차례 쓰기 20건(방 `preflight-probe`): 201 × 20, X-Served-By onprem × 20, p50 588 · p95 724 · 최대 833 ms | D-30: WARN 1,000 · FAIL 1,500 ms(SHIFT `BRAVO_FRONT_MAX_P95_MS`). 20건 p95는 참고값(SHIFT 표본은 100건) |
| `repl` 역방향 | 출발지 `bravo_rev_sub` 꺼짐, slot_name NONE, 온프렘 역방향 슬롯 없음 | 슬롯이 있거나 slot_name이 있으면 FAIL(Executor도 REVERSE_SLOT_PRESENT로 거부) |
| `pool` | 출발지 · 온프렘 모두 `chat-pgbouncer-target=src`, 활성 대상 = `target-src.ini` | 하나라도 다르면 FAIL |
| `kube` 게이트 | 컨테이너 1개 Ready, 재시작 0, OOMKilled 0, 노드 OOMKilling 이벤트 0, 한도 1Gi, 사용 65Mi | OOMKilled · 이벤트 있으면 FAIL, 재시작만 있으면 WARN, 한도가 1Gi가 아니면 WARN |
| `app` | spec · 실행 중 · 게이트 VAP 고정 값이 세 클러스터 모두 `sha256:76e8f595…`(= `deploy/landing-zone/versions.env`) | 하나라도 다르면 FAIL |

측정 도구 결함 하나를 고쳤습니다. 첫 구현은 psycopg 기본값으로 6번째 질의부터 서버 쪽 prepared statement를 만들었고, PgBouncer transaction 모드에서 공유되는 서버 연결에 남아 다음 측정이 `DuplicatePreparedStatement`로 실패했습니다(04:08 실행, 표는 지움). 지금은 앱과 같이 `prepare_threshold=None`입니다. 앱은 처음부터 `prepare_threshold=None`이라 남은 이름과 부딪히지 않습니다.
