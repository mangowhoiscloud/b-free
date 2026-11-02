# 0007. 키와 비밀은 플랫폼 금고 하나만 보관

- 상태: 채택. 저장 데이터 암호화의 루트 키(KMS CMK) 보관은 **제안**(열림)
- 날짜: 2026-11-02
- 정한 사람: 류지환(팀장). 원문: "키 관리는 모두 우리 플랫폼이 맡는거야. 다른 쪽이 가면 안돼."
- 스펙 위치: §7.2, §7.3, §7.5, §8.4, §8.8, NFR-6, D-26
- 근거 문서: `docs/spec/DEVIATIONS.md` D-26, `deploy/CLOUD-E2E-CONTRACT.md` 1 · 4 · 8 · 9절, `deploy/pilot/README.md` "플랫폼 금고", `deploy/pilot/scripts/keys.sh`, `deploy/pilot/scripts/executor-bao.sh`

## 맥락

스펙은 목적지마다 OpenBao + External Secrets를 둡니다(§7.2, §7.3). 서비스 토큰과 터널 토큰의 보관 위치는 정하지 않았습니다. 앞 설계(D-16)는 부트스트랩 스크립트가 토큰을 AWS Secrets Manager에 쓰고, 온프렘 토큰은 운영자가 온프렘 OpenBao에 손으로 넣게 했습니다. 이러면 키가 고객 클라우드 금고, 온프렘 금고, 노트북 `.env`에 흩어집니다.

## 선택지

| 안 | 장점 | 단점 |
|---|---|---|
| A. 목적지마다 OpenBao(스펙) | 목적지가 금고를 직접 가짐 | 금고가 고객 환경 수만큼 늚. 키가 고객 쪽으로 감 |
| B. 공급자 금고 연결(EKS면 Secrets Manager + ESO) | 관리형 | 고객 클라우드에 우리 키가 감. 환경마다 금고가 다름 |
| C. 플랫폼 OpenBao 하나. 고객 클러스터는 ESO만, 그 환경 경로의 값만 당겨 옴 | 키가 한 곳. 고객 쪽에는 값만 잠깐 | 플랫폼 금고가 단일 장애점. 링크가 서기 전에는 플랫폼이 값을 파이프로 넘겨야 함 |

## 결정

C를 고릅니다.
- 보관 주체는 플랫폼 OpenBao 하나입니다. 경로는 `secret/platform/*`, `secret/envs/<env>/*`입니다.
- 고객 클러스터에는 금고를 두지 않고 ESO만 둡니다. ESO가 플랫폼 OpenBao를 당겨 오는 방식은 링크가 선 뒤에 켭니다(계약 4절). 그전에는 플랫폼이 값을 파이프로만 `kubectl create secret ... --from-file=<key>=/dev/stdin`에 넣습니다.
- Cloudflare API 토큰은 플랫폼 OpenBao에 있습니다. `.env`는 첫 부트스트랩에만 쓰고 바로 비웁니다(`keys.sh import-cloudflare`).
- 터널 토큰과 Access 서비스 토큰은 플랫폼 키 단계가 Cloudflare API에서 받아 OpenBao에 바로 씁니다(`keys.sh issue-tunnel-token`). Terraform 상태와 출력에는 비밀 값이 없습니다.
- Executor와 리허설 서비스는 AppRole로 실행 동안만 읽습니다. secret_id는 60초 1회용입니다(`executor-bao.sh with-executor`).
- CI는 저장소 비밀 없이 OIDC만 씁니다. 에이전트 · Harbor 샌드박스와 프롬프트에는 비밀 값을 주지 않습니다.

**예외.** 출발지 AWS 앱의 Secrets Manager 비밀은 고객의 기존 상태입니다. 플랫폼 키가 아니라 이전 때 플랫폼 금고로 옮길 대상입니다(S2, T9).

## 결과

- 쉬워지는 것: 키를 회전 · 폐기할 곳이 하나입니다. 고객 환경을 지워도 키가 남지 않습니다.
- 어려워지는 것: 플랫폼 금고가 멈추면 새 실행이 시작되지 않습니다. 보관 파일(`~/.bravo/custody/openbao-init.json`) 하나로 금고 전체가 열립니다(감사 44: devauth 키와 금고 보관 파일이 한 곳에 몰림).
- 2026-11-02 상태: 금고는 노트북의 `platform-bao` 컨테이너입니다. 플랫폼 EKS로 옮기는 동안의 예외입니다(계약 6.1). OpenBao는 노트북 127.0.0.1에만 열려 있습니다.
- LLM 자격도 이 원칙을 따릅니다. 데모의 팀장 구독 토큰과 OpenAI API 키도 플랫폼 OpenBao에만 있고, 워커가 실행 때 한 번 읽습니다(ADR 0009).
- 열린 것(제안): 저장 데이터 암호화의 루트 키(클라우드 KMS CMK)까지 플랫폼이 쥘지 정하지 않았습니다. 후보는 가져온 키 재료(BYOK)나 외부 키 저장소(XKS)입니다.
- 되돌리려면: ESO의 SecretStore를 환경별 금고로 바꿉니다. 앱이 받는 Secret 이름은 그대로입니다.
