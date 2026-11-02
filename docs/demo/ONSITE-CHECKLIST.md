# 현장 점검표(11/7 중간 보고 · 11/8 최종 발표)

라이브 데모가 미리 확인할 수 있는 일로 멈추지 않게 하는 점검표입니다(리뷰 24 B16–B20, P6, P9, P23). 장면 순서는 `SCRIPT.md`, 기능 점검은 `INTEGRATION-CHECK.md`가 다룹니다. 이 문서는 장비, 자격 증명, 시작 상태만 다룹니다.

**쓰는 법.**
- 명령은 저장소 뿌리에서 돌립니다. 표시가 없는 명령은 읽기만 합니다. "쓰기"로 표시한 명령만 무엇을 바꿉니다.
- 모든 판정은 `deploy/scripts/preflight.sh`의 PASS · WARN · FAIL입니다. FAIL이 하나라도 있으면 5절대로 고친 뒤 다시 돌립니다.
- 결과는 `--out docs/evidence/demo-ops/preflight-<UTC 시각>`으로 남깁니다. 비밀 값은 출력에 없습니다.

## 1. 하나에 몰린 장애 영역(알고 들어감)

| 무엇 | 왜 위험한가 | 대비 |
|---|---|---|
| 운영 Mac 한 대 | 온프렘 RKE2(Lima VM 2대), 플랫폼 kube API(SSM 포트 포워드 127.0.0.1:26443), 키 보관 파일(`~/.bravo/custody`)이 모두 이 Mac에 있음. Mac이 잠들거나 꺼지면 온프렘 목적지 전체와 플랫폼 kubectl이 멈춤 | 2절. 이 Mac은 무대 내내 켜 두고 덮개를 닫지 않음. 키 보관 파일은 다른 장비로 옮기지 않음 |
| 플랫폼 EC2 1대(RKE2 단일 노드) | 콘솔 · API · Executor · 게이트 · OpenBao가 한 노드. 노드가 죽으면 S2–S4 전체가 멈춤. 복구 시간은 잰 적 없음 | 무대에서는 5절의 대체 장면(사전 실행 기록)으로 넘어감 |
| Cloudflare | 사용자 진입(`b-free`, `b-free-chat*`), Access 로그인, D-16 링크 20개가 모두 Cloudflare를 지남 | 다른 길은 없음(설계, D-16). 장애면 사전 실행 기록으로 넘어가고 장애라고 말함 |
| OpenBao 링크 `t1-plat-bao` | 끊기면 온프렘 RGW 암호화 첨부의 PUT · GET이 500(11/3 측정) | preflight `rgw` · `link` 그룹. RGW 토큰 회전은 무대 밖에서 끝냄 |

## 2. 장비와 자리(도착하자마자)

1. 전원. Mac에 충전기를 꽂음. 멀티탭 하나를 우리 자리 전용으로 씀. preflight `Mac 전원`이 `AC Power`여야 함.
2. 절전 끄기. 터미널 하나에서 `caffeinate -dimsu`를 띄워 두고 닫지 않음. 또는 `sudo pmset -a sleep 0 disksleep 0`(쓰기, 끝나면 `sudo pmset -a sleep 1`로 되돌림). 덮개를 닫지 않음. preflight `Mac 절전`이 PASS여야 함.
3. 네트워크. 유선 LAN(USB-C 어댑터)을 먼저 씀. Wi-Fi는 보조, 휴대폰 테더링은 예비. 회장 네트워크가 Cloudflare(443, 7844)와 AWS SSM(443)을 막지 않는지 preflight `edge` · `link` · `kube`로 봄.
4. 네트워크를 바꾼 뒤에는 SSM 포트 포워드를 다시 띄움(쓰기 아님, 세션만):
   `aws ssm start-session --region ap-northeast-2 --target <플랫폼 EC2 instance_id> --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["6443"],"localPortNumber":["26443"]}'`
   (instance_id는 `deploy/platform/README.md`의 kube API 줄). 그다음 `kubectl --context bravo-platform-rke2 get nodes`.
5. Lima VM. `limactl list`에서 `bravo-onprem-server`, `bravo-onprem-agent`가 `Running`. Mac을 다시 시작하지 않음. VM 재시작 뒤 RKE2 · Ceph가 다시 준비되는 시간은 잰 적 없음.
6. 이 Mac에서 부하 생성기(loadgen)를 돌리지 않음. 11/3 02:44 KST에 노트북 loadgen이 TIME_WAIT 소켓 약 9,400개를 남겼고, kubectl과 curl이 `can't assign requested address`로 실패했음. preflight `Mac TCP 포트`가 PASS여야 함.
7. 예비 노트북은 브라우저 전용(콘솔 두 신원, 사전 실행 기록 화면). 키 보관 파일과 kubeconfig는 옮기지 않음.

## 3. 데모 전날(11/6, 늦어도 11/7 오전)

| # | 하는 일 | 명령 | 기대 |
|---|---|---|---|
| 1 | 자격 증명 만료 표 | `deploy/scripts/rotate-credentials.sh plan` | 11/9 06:00 KST 전에 끝나는 것 없음. 11/3 03:0x 측정은 4절 표 |
| 2 | 미리 회전(쓰기) | `deploy/scripts/rotate-credentials.sh run --before 2026-11-10T06:00:00+09:00 --log docs/evidence/demo-ops/rotation-<UTC 시각>` | 서비스 토큰 20개 · TokenRequest 토큰 4개 · custody 토큰이 회전한 날 + 7일로 밀림. 요약 `FAIL 0`. 서비스 토큰마다 약 40초(전파 대기 30초 + 링크 재시작, 11/3 측정), 모두 약 15분. 무대 밖에서만 |
| 3 | RGW 토큰(쓰기, 첨부 약 40초 멈춤) | `deploy/scripts/rotate-credentials.sh run --kinds rgw-kms --before 2026-11-10T06:00:00+09:00` | `rgw-kms: GET … HTTP 200`. 11/3 측정 최대 수명 끝 11/10 00:41 KST |
| 4 | Cloudflare API 토큰 | 사람이 대시보드에서 새 토큰을 만들고 `deploy/pilot/scripts/keys.sh import-cloudflare` | preflight `Cloudflare API 토큰` 만료가 11/9 23:59 KST보다 뒤 |
| 5 | 두 신원 첫 로그인 | naver(운영자) · gmail(승인자)로 `https://b-free.mangowhois.ai` 로그인(One-time PIN) | preflight `operator 신원(@naver.com)` · `approver 신원(@gmail.com)` PASS. 11/3 02:58 KST에는 naver 신원이 플랫폼 DB에 없었음(FAIL) |
| 6 | 동결(쓰기: 로컬 태그) | `deploy/scripts/demo-freeze.sh create` → 기록 폴더를 커밋, 팀장이 `git push origin <태그>` | 전제가 안 맞으면 만들지 않고 이유를 냄. 기록은 `docs/evidence/freeze/<태그>/`(11/3 시험 생성 · 비교 각 6–9초) |
| 7 | 리허설 뒤 되돌리기 | `deploy/scripts/reset-demo.sh --dry-run --plan <리허설 계획 ID>` → PLAN이 있으면 같은 명령에서 `--dry-run`을 빼고 실행(쓰기) | 요약 `FAIL 0`, `NOT_CONNECTED 0`, 다시 dry-run하면 모두 `ALREADY` |

동결 중 규칙: main에 `deploy/` 변경을 push하지 않습니다. 꼭 고쳐야 하면 고치고 6번을 다시 해서 새 태그를 만듭니다. Argo CD를 태그에 묶는 방법(`deploy/platform/argocd/apps/platform.yaml`의 `targetRevision`)은 팀장이 정합니다.

## 4. 11/3 측정한 자격 증명 만료(03:0x KST, `rotate-credentials.sh plan`)

처음 끝나는 것은 D-16 링크 서비스 토큰 8개(11/9 20:50 KST)와 custody · TokenRequest 토큰(11/10 02:53 KST)입니다. 데모(11/7 · 11/8)는 모두 그 전입니다. 여유는 하루뿐이라 3절 2번을 11/6에 합니다.

| 자격 | 만료(KST) | 회전 방법 |
|---|---|---|
| 링크 서비스 토큰 `plat-src-*` · `plat-dst-db/k8s/pgb` · `src-dst-db` · `dst-src-db` | 11/9 20:50 | `rotate-credentials.sh one service-token <env>/<secret>` |
| `gke-*` 3개, `src/dst-plat-otel`, `plat-src/dst-admin`, `plat-dst-s3` | 11/9 21:55–22:50 | 같음 |
| `dst-plat-bao`(RGW → OpenBao 링크) | 11/10 00:23 | 같음. 소비자 `rook-ceph/plat-bao-link` 재시작 동안 RGW 암호화 첨부가 잠깐 멈출 수 있음 |
| RGW SSE-KMS 토큰 최대 수명(168h) | 11/10 00:41 | `rotate-credentials.sh one rgw-kms`(RGW 재시작) |
| Cloudflare API 토큰(플랫폼이 LB 가중치를 바꿀 때 씀) | 11/9 23:59 | 사람(3절 4번) |
| TokenRequest 토큰 4개(수집기 · Executor · 리허설 · 게이트) | 11/10 02:53(11/3 회전 연습 뒤) | `rotate-credentials.sh one k8s onprem` · `one k8s gate` |
| custody 주기 토큰 5개 | 11/10 02:53(쓸 때마다 168h로 연장) | `rotate-credentials.sh one custody` |
| `llm-openai` 접근 토큰 | 11/12 21:31(워커가 refresh_token으로 갱신) | 사람(`keys.sh llm-login openai`) |
| `llm-claude` | 값을 써야 알 수 있음. 사용량 한도도 알 수 없음 | 사람 |

RGW 토큰은 TTL 1시간이고 CronJob `rook-ceph/rgw-kms-renew`가 15분마다 갱신합니다. 갱신이 1시간 멈추면 첨부 PUT · GET이 500입니다. preflight `RGW 토큰 갱신`이 마지막 성공 20분 안이면 PASS입니다.

## 5. T-60분

1. 2절 1–6을 다시 봄.
2. 두 브라우저 프로필로 새로 로그인합니다. 운영자는 naver, 승인자는 gmail입니다. 콘솔 세션은 24시간이고, 로그인은 One-time PIN 메일이 와야 됩니다. 회장 Wi-Fi에서 메일이 늦을 수 있어 T-60에 끝냅니다. 콘솔 위쪽의 역할 표시가 각각 operator, approver인지 봅니다.
3. 전체 점검:
   `deploy/scripts/preflight.sh --out docs/evidence/demo-ops/preflight-$(date -u +%Y%m%dT%H%M%SZ)`
   2분 안에 끝납니다(11/3 측정 13–32초). FAIL 0이 목표입니다. 세션까지 보려면 각 브라우저의 `CF_Authorization` 쿠키 값을 `PREFLIGHT_OPERATOR_COOKIE`, `PREFLIGHT_APPROVER_COOKIE`에 넣고 돌립니다(값은 출력 · 파일에 남지 않음). 넣지 않으면 그 두 줄은 WARN이고, 2번에서 눈으로 본 것으로 갈음합니다.
4. 되돌리기 확인: `deploy/scripts/reset-demo.sh --dry-run --plan <직전 리허설 계획 ID>`. 모두 `ALREADY`여야 합니다. PLAN이 있으면 `--dry-run`을 빼고 돌립니다(쓰기, 11/3 측정 깨끗한 상태에서 9초).
5. 동결 확인: preflight `freeze` 줄 또는 `deploy/scripts/demo-freeze.sh check`. 이미지 digest · ConfigMap이 동결 기록과 같아야 합니다.

## 6. T-10분

1. 빠른 점검(약 15초): `deploy/scripts/preflight.sh --only kube,link,repl,rgw,http,console,host`
2. 시작 상태: preflight `LB pool bravo-b-free-chat-src` 가중치 1, `bravo-b-free-chat-dst` 0, `b-free-chat.mangowhois.ai` `env.id=aws`, 심은 퇴행 `planted`.
3. 정방향 복제: preflight `정방향 구독 bravo_fwd_sub`의 `latest_end_time`이 45초 안. 역방향 슬롯 남은 여유가 512MB 위.
4. 열어 둘 화면: 콘솔 두 창(운영자, 승인자), 사전 실행 기록(`docs/evidence/`), Grafana, 휴대폰 채팅 창.

## 7. FAIL이면

| preflight 줄 | 뜻 | 할 일 |
|---|---|---|
| `kube platform API` 응답 없음 | SSM 포트 포워드가 끊김 | 2절 4번 |
| `kube onprem API` 응답 없음 | Lima VM이나 Mac 문제 | `limactl list`, 멈췄으면 `limactl start bravo-onprem-server`, `limactl start bravo-onprem-agent`. 준비 시간 미측정: 무대 직전이면 대체 장면 |
| `bao 봉인 상태` sealed=True | OpenBao 재시작 뒤 봉인 | `deploy/pilot/scripts/keys.sh unseal`(쓰기, custody 필요) |
| `argo 앱 …` FAIL | 동기화 실패 · 건강 아님 · main보다 뒤 | Argo CD 화면에서 원인 확인. 동결 뒤라면 누가 push했는지부터 봄 |
| `cred …` FAIL(2시간 안에 만료) | 자격 증명 만료 직전 | 4절 표의 회전 방법. RGW · 링크 재시작이 끼면 무대 밖에서 |
| `link …` FAIL | 그 링크로 상대 서비스가 답하지 않음 | `kubectl -n <ns> logs deploy/<링크>`. `bad handshake`면 토큰 문제(회전), 아니면 노출 쪽 터널(`edge` 터널 줄)과 상대 서비스 확인 |
| `repl 정방향 구독` FAIL · WARN | 복제가 링크에서 끊겼거나 늦음 | 온프렘 `chat/src-db-link` 확인 · 재시작. 다시 45초 안으로 들어오는지 preflight `--only repl` |
| `repl 슬롯 bravo_rev_sub` WARN · FAIL | 쓰지 않는 역방향 슬롯이 WAL을 쌓음. 한도(2GB)를 넘으면 슬롯이 사라져 S4 되돌리기가 안 됨 | `reset-demo.sh --plan …`의 data 단계(`repl_setup rearm`이 슬롯을 다시 만듦). 11/3 03:0x 측정 보존 464MB, 여유 1.6GB |
| `rgw 암호화 객체 읽기` FAIL | RGW가 OpenBao 링크 · 토큰으로 복호화하지 못함 | `link onprem rook-ceph/plat-bao-link` 줄과 `RGW 토큰 갱신` 줄을 먼저 봄 |
| `edge LB pool` 가중치가 시작값과 다름 | 앞선 시험이 전환을 남김 | `reset-demo.sh --plan … --only front`(쓰기) |
| `http b-free-chat` env.id가 aws가 아님 | 위와 같음, 또는 출발지 앱 문제 | 위와 같음. 이미 src인데 실패면 출발지 앱 확인(reset이 전환하지 않음) |
| `console operator 신원` FAIL | 운영자가 한 번도 로그인하지 않음 | naver 계정으로 첫 로그인 |
| `host Mac 절전` · `Mac TCP 포트` | 잠들 수 있음 · 소켓이 넘침 | `caffeinate -dimsu`, 이 Mac의 loadgen 중지 |
| `freeze` FAIL | 동결 뒤 배포가 바뀜 | `demo-freeze.sh check` 출력의 워크로드를 보고 되돌리거나 새 태그 |
| `s1 심은 퇴행` WARN | S1의 BLOCK 장면에 필요한 열린 버킷이 없음 | `deploy/cloud/rgw_regression.py --context bravo-onprem --plant-regression`(쓰기) |

무대 중 장애면 고치지 않고 대체 장면(`SCRIPT.md` 5절, 사전 실행 기록)으로 넘어갑니다. 실행 ID와 시각을 함께 보입니다.
