# 0005. 객체 저장은 같은 S3 계약 + 환경별 엔진(온프렘 Rook-Ceph RGW)

- 상태: 채택(조건부). SeaweedFS 제외와 "같은 계약 + 환경별 엔진"은 류지환 결정. 온프렘 RGW는 11/3 실측이 실패하면 다시 정함. 온프렘 블록 저장은 **제안**(결정 전)
- 날짜: 2026-11-02
- 정한 사람: 류지환(팀장)
- 스펙 위치: FR-3, FR-4, §6 S1 · S2, §7.2, §7.3, §8.3, §8.4, §8.8, §9, §12, T9, D-22
- 근거 문서: `docs/spec/DEVIATIONS.md` D-22, `docs/spec/TECH-STACK-SPEC.md` 2.3절 · 2.8절, `docs/architecture/11-landing-zone.md` 2절, 리서치 `research/platform-research/object-storage-cdn-2026-11-02.md`(저장소 밖)

`DEVIATIONS.md` 요약 표의 D-22 행은 "제안(검증 중). SeaweedFS 제외만 류지환 결정"이고, 본문 상태 줄은 "같은 S3 계약 + 환경별 엔진"까지 류지환 결정입니다. 이 ADR은 본문과 `TECH-STACK-SPEC.md` 2.3절을 따릅니다. 표 행은 문서 정리 대상입니다.

## 맥락

스펙은 목적지 객체 저장을 SeaweedFS로 둡니다. SeaweedFS는 재단 없는 단일 프로젝트라 채택 근거를 말하기 어렵습니다. 설계가 이미 기대는 요구는 이렇습니다.
- S3 API(PUT, GET, HEAD, LIST). 앱과 첨부 복사가 S3 API만 씁니다.
- 버킷 정책: 익명 거부, 앱 역할만 읽기 · 쓰기. 게이트 1차 BLOCK과 S2 번역의 대상입니다.
- 버킷 하나에 묶인 자격, 종단 TLS, arm64 Lima VM 2대에 들어가는 자원.

## 선택지

| 안 | 판정 | 이유 |
|---|---|---|
| A. SeaweedFS(스펙) | 제외 | 채택 근거 약함 |
| B. 모든 목적지에 같은 엔진(Rook-Ceph RGW) | 폴백 | 엔진까지 같지만, 클라우드 위 Ceph는 관리형 블록 저장 위에 복제를 다시 얹음. GKE Autopilot은 특권 · hostPath를 막음 |
| C. 같은 S3 계약 + 환경별 엔진: 온프렘 RGW, EKS S3, GKE Cloud Storage | 채택 | 앱 매니페스트, 환경 변수 이름, 버킷 이름, 자격 Secret 이름, 통제 질문 ID가 세 환경에서 같음. 다른 것은 overlay의 엔드포인트와 자격 출처뿐 |
| D. MinIO, Garage, Cloudflare R2 | 제외 | MinIO 저장소 보관 상태 · 단일 노드 약관, Garage 버킷 정책 없음, R2 버킷 정책 · 일본 데이터 위치 보장 없음 |

## 결정

C를 고릅니다. 게이트는 정책 문서가 아니라 행동을 시험합니다. OBJ-01 서명 없는 GET 거부, OBJ-02 LIST 거부, OBJ-03 PUT 거부, OBJ-04 앱 자격 읽기 · 쓰기 허용(대조군), OBJ-05 다른 자격 거부, OBJ-06 평문 HTTP 거부입니다. S2 제안서의 `target.type`은 온프렘 `rgw_bucket_policy`, GKE `gcs_iam_policy`, EKS `s3_bucket_policy`입니다. 첨부는 캐시하지 않습니다.

## 결과

- 쉬워지는 것: 클라우드 목적지에서 Ceph를 운영하지 않습니다. 초보 고객이 클라우드마다 저장소를 운영하지 않습니다.
- 어려워지는 것: 엔진이 달라 버킷 정책 번역이 방향마다 다릅니다. 동등성은 OBJ-01–06 행동 시험으로만 판단합니다. RGW의 TLS 전용 정책은 `BoolIfExists`로 써야 합니다(`TECH-STACK-SPEC.md` 2.8절).
- 2026-11-02 20:45 KST 상태: 온프렘 RGW 설치는 아직 없습니다. 랜딩 존 설치기 단계에 RGW가 없고, 판만 `deploy/landing-zone/versions.env`에 있습니다(Rook 차트 v1.21.1). 그래서 데모 S1의 "열린 첨부 저장소로 BLOCK" 장면은 준비 중입니다(`docs/demo/SCRIPT.md`). 게이트 OBJ 시험에는 감사 A5(OBJ-06 시간 초과도 PASS, OBJ-05 잘못된 자격도 PASS)와 B17(OBJ-02 · 03 canary 없음)이 열려 있습니다.
- 온프렘 블록 저장은 정하지 않았습니다. 임시로 K3s `local-path`가 `bravo-db`, `bravo-general`의 바탕입니다. 후보는 Rook-Ceph RBD입니다.
- 되돌리려면: 팀장이 D-23을 "엔진까지 같음"으로 정하면 B로 갑니다. 앱 쪽 계약은 그대로입니다.
