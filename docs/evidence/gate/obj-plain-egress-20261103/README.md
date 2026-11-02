# OBJ-PLAIN-EGRESS: GKE OBJ-06 예외의 대체 통제를 재는 질문 (2026-11-03 02:45–02:55 KST)

리뷰 P10 · 원장 G3. GKE OBJ-06은 FAIL입니다. GCS XML API가 평문 HTTP를 받고, 조직 없는 프로젝트라 `storage.secureHttpTransport`를 걸 수 없습니다(`../obj06-gke-orgpolicy-20261102T143014Z/`). 대체 통제는 "앱 쪽에서 평문으로 저장소에 닿지 못한다"입니다. 이것을 게이트 질문 `OBJ-PLAIN-EGRESS`(ENC-01)로 만들었습니다.

## 질문

앱 Deployment의 라벨 · 이미지로 띄운 시험 Pod(NET-PATHS와 같은 Pod, 같은 입장 정책 `bravo-gate-netprobe`)에서:

1. 저장소 TLS 포트 연결 성공(대조. 실패하면 UNKNOWN)
2. 저장소 평문 포트 연결 실패(거부 · 시간 초과). 연결되면 FAIL
3. 앱 컨테이너의 `S3_ENDPOINT_URL`이 `https://`(미설정은 SDK 기본 https). `http://`면 FAIL

| 환경 | TLS 경로 | 평문 경로 | 앱 엔드포인트 | 결과 |
|---|---|---|---|---|
| 출발지 EKS | `s3.ap-northeast-2.amazonaws.com:443` 연결 | `:80` 시간 초과 | 미설정(SDK 기본 https) | PASS |
| 온프렘 RKE2 | `rook-ceph-rgw-bravo.rook-ceph.svc:443` 연결 | `:80` 시간 초과 | `https://rook-ceph-rgw-bravo.rook-ceph.svc` | PASS |
| GKE | `storage.googleapis.com:443` 연결 | `:80` 시간 초과 | `https://storage.googleapis.com` | PASS |

원본: `../platform-gate-20261102T174659Z/`, `../platform-gate-20261102T175426Z/`(GKE 앱을 같은 digest로 바꾼 뒤).

## 바꾼 것

- **GKE.** 바꿀 것이 없었습니다. `chat-bravo-demo` NetworkPolicy가 바깥으로 443만 엽니다(`deploy/landing-zone/overlays/gke/values/chat.yaml`). 측정으로 확인했습니다.
- **온프렘.** 앱 egress가 `rook-ceph` 네임스페이스로 80 · 8080 · 8443 · 443을 열고 있었습니다. 앱 Pod에서 RGW `:80`이 연결됐습니다(`01-onprem-egress-patch.txt`, 02:45 KST). 443만 남겼습니다. 값 파일 `deploy/landing-zone/overlays/rke2/chat-values.yaml`을 고치고, 돌고 있는 Helm 관리 NetworkPolicy는 같은 내용으로 `kubectl patch` 했습니다(앱 Pod 재시작 없음). 다음 `install.sh rke2 … app`의 Helm 업그레이드가 같은 값을 씁니다. 바꾼 뒤 앱 Pod에서 443 연결, 80 시간 초과.
- **출발지.** 바꿀 것 없음(바깥 443만).

## 예외 요청에 적을 것

예외는 콘솔에서 사람이 요청 · 승인합니다(이 갈래는 만들지 않음). `compensating_control`에는 질문 ID를 적습니다. 예:

> OBJ-PLAIN-EGRESS PASS(target-gcp): 앱 라벨 Pod에서 storage.googleapis.com:80 차단, 앱 엔드포인트 https. 노드 바깥 80 방화벽.

예외가 묶이는 OBJ-06 목적지 증거 digest(02:55 KST 실행): `sha256:15cd6d0aa80287f1f7dadce15d1cc91d38fb9d4ea146d4fabd46adcdba00fc62`. 계획 평가 때의 실제 digest는 그 실행의 값입니다. 대체 통제가 FAIL로 바뀌어도 지금 API는 예외를 자동으로 무르지 않습니다(예외는 OBJ-06 증거에만 묶임). 묶는 것은 남은 일입니다.
