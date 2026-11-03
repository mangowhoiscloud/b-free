# T14 · T24 팀 공유 전 점검 — 2026-11-03

## 공개 범위와 점검

개인 `b-free` 구현·문서·실측 증거를 공개합니다. 팀 저장소로 코드를 이전하거나 팀 인수 완료를 선언하는 작업은 포함하지 않습니다.

| 범위 | 결과 |
|---|---|
| 기존 Git 전체 이력 96개 커밋 | gitleaks `git --log-opts=--all --redact`: 탐지 0 |
| 공유 작업 트리, 압축 파일 깊이 3 | 약 50.70MB 검사. 탐지 3건 모두 마스킹 시험의 고정 가짜 AWS 키 문자열 |
| 기존 GitHub Actions 159개 실행, 로그 파일 1,392개 | 전부 내려받아 검사. 탐지 17건 모두 임시 CI PostgreSQL 암호: link fixture 5건·gate fixture 12건 |
| CI 암호 확인 | `tools/link/tests/conftest.py`는 실행별 UUID, `platform/gate/tests/conftest.py`는 실행별 난수 암호를 생성. 임시 Docker DB와 loopback 연결, 종료 시 컨테이너 정리. 운영 클라우드 키 아님 |
| 플랫폼 비밀 | OpenBao 초기화·봉인 해제 확인, root token 폐기·감사 활성화. Cloudflare·LLM 값은 OpenBao, 실행 시 Kubernetes Secret/단기 전달 |
| GitHub Actions 비밀 | 저장된 repository secret 없음. CI는 GitHub OIDC와 `GITHUB_TOKEN` 사용 |

자동 탐지 결과와 코드 대조 범위를 기록한 것이며, 모든 형태의 비밀 부재를 보증하는 전수 증명은 아닙니다. 검사 원본과 실제 이메일 목록은 공개 저장소에 포함하지 않습니다.

## 데모 readback

11:18 KST 로그인한 콘솔에서 AWS EKS 2/2, 온프렘 RKE2 2/2, GKE 1/1 노드와 플랫폼 서비스 응답을 확인했습니다. 당시 계획·승인 기록은 0건입니다. 비용 일부 출처·콘솔 로그 및 트레이스 목록은 미연결입니다. 이는 조회 확인이며 승인→컷오버→복구의 E2E 실행 기록이 아닙니다.

## 접근 설정

기존 운영자·승인자 신원을 유지하고 팀원 네 명에 대해 Cloudflare 이메일 허용 및 플랫폼 첫 로그인 `viewer` 역할을 설정했습니다. 실제 멤버 목록은 OpenBao `secret/envs/platform/access-members` → Kubernetes Secret `bravo-control/access-members`로 전달합니다. 팀원의 PIN 수신·로그인 완료는 각자가 사용한 뒤에 확인할 수 있습니다.

문서 검사 `make check`, 셸 구문 검사, Kustomize 렌더링에서 API·MCP 두 워크로드가 접근 Secret을 참조하며 예시 이메일이 배포 결과에 없음을 확인했습니다.
