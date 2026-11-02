# platform/audit

감사 기록 전달기입니다(T24, P1, FR-14). 플랫폼 API의 이벤트를 S3 Object Lock 버킷에 한 건씩, 고칠 수 없게 남깁니다. LLM을 쓰지 않습니다(NFR-5).

## 무엇을 남기나

- 출처는 플랫폼 API의 계획별 이벤트 흐름 `GET /v1/plans/{id}/events`(SSE, `deploy/pilot/CONTRACT.md` 7절)입니다. `platform/api` 내부는 고치지 않고 이 흐름만 읽습니다.
- 모든 종류를 남깁니다: 상태 전이(`state.changed`), 승인(`approval.granted`), 게이트 판정(`gate.verdict`), 실행 단계와 결과(`run.stage`, `run.done`), 영수증(`receipt.saved`), 막힌 에이전트 시도(`tool.denied`), 리허설 이벤트.
- 객체 키는 `events/<plan_id>/<event_id 12자리>.json`, 본문은 API 이벤트 그대로의 정규 JSON입니다. 메타데이터 `sha256`, PUT마다 `x-amz-checksum-sha256`.
- PUT마다 `ObjectLockMode`와 `ObjectLockRetainUntilDate`를 줍니다. 버킷 기본 보존도 같이 걸립니다.
- 같은 키를 다시 쓰지 않습니다. 내용이 같으면 `exists`, 다르면 `conflict`로 멈춥니다(변조 또는 API 변경).
- 다시 시작하면 버킷에 있는 가장 큰 이벤트 ID를 `Last-Event-ID`로 보내 이어 받습니다. 로컬 상태 파일이 없습니다.

## 버킷

`bootstrap-bucket.sh <이름> [리전]`이 만듭니다. 출발지 AWS 계정에 `component=platform` 태그로 둡니다. 모든 설정을 S3 API로 하므로 `BRAVO_AUDIT_S3_ENDPOINT`를 주면 Ceph RGW에도 같은 명령이 갑니다.

| 설정 | 값 |
|---|---|
| Object Lock | 기본 보존 COMPLIANCE, `BRAVO_AUDIT_RETENTION_DAYS`(기본 1일). 보존 중에는 root도 지우거나 줄이지 못함. 데모 뒤 버킷을 비우려면 마지막 보존이 끝나야 함 |
| 버전 관리 | 켬(Object Lock의 조건) |
| 공개 차단 | 네 항목 모두 켬 |
| 암호화 | SSE-S3(AES256) |
| 버킷 정책 | TLS 없는 요청 거부. `BoolIfExists`라서 RGW(평문 요청에 `aws:SecureTransport` 키가 없음)에서도 거부 |

2026-11-02 20:40 KST에 `bravo-platform-audit-b9c8af1e6b4c`(ap-northeast-2)를 만들었습니다.

## 실행

    uv sync --frozen
    export BRAVO_AUDIT_BUCKET=bravo-platform-audit-b9c8af1e6b4c BRAVO_API_BASE=<플랫폼 API 주소>
    export BRAVO_AUDIT_TOKEN=...   # 콘솔 AUD의 Access JWT. EKS에서는 OpenBao → ESO로만 받음
    uv run --frozen python -m bravo_audit check-bucket
    uv run --frozen python -m bravo_audit follow --plan <plan_id> [--plan <plan_id> ...] [--idle-exit 30]
    uv run --frozen python -m bravo_audit verify --plan <plan_id>    # sha256, 잠금 방식, 보존 기한 다시 확인

S3 자격은 표준 AWS 체인입니다(EKS에서는 Pod Identity). 이미지는 uid 10001로 돕니다.

## 시험

    BRAVO_TEST_AUDIT_BUCKET=<Object Lock 버킷> uv run --frozen pytest -q

- `tests/test_sse.py`: API 흐름 형식(id, data, 빈 줄, `: ping`) 해석.
- `tests/test_sink.py`: 실제 Object Lock 버킷에 두 건을 쓰고 다시 씀(`exists`), 내용을 바꿔 씀(`conflict`), 재개 지점, `verify`, 잠긴 버전 삭제가 거부되는지 확인. 매 실행이 `events/test-*/`에 작은 객체 두 개를 남기고, 보존 기간 동안 지울 수 없습니다. 변수가 없으면 건너뜁니다.

## 아직 안 된 것

- 실제 플랫폼 API에 붙여 보지 못했습니다. 노트북 Compose를 내렸고, 플랫폼 EKS의 API는 아직 서지 않았습니다.
- API에 계획 목록 엔드포인트가 없어 계획 ID를 인자로 줍니다. 새 계획을 스스로 찾지 못합니다.
- HTTP 단계에서 거부된 요청(403 `security_hold`, 409 멱등 충돌)은 API가 이벤트로 남기지 않아 여기에도 없습니다. API 쪽 이벤트 추가가 필요합니다.
