# 0006. 온프렘 · GKE · EKS에 같은 랜딩 존

- 상태: 채택. 해석은 "같은 계약 · 같은 경험, 엔진은 달라도 됨"(팀장 결정 2026-11-02)
- 날짜: 2026-11-02
- 정한 사람: 류지환(팀장) 결정, 회의 공유
- 스펙 위치: §4, §7.2, §7.3, §8.8, D-23(D-01과 연결)
- 근거 문서: `docs/spec/DEVIATIONS.md` D-23, `docs/architecture/11-landing-zone.md`, `docs/architecture/10-cluster-overview.md`, `docs/spec/ALIGNMENT-2026-11-02.md` 1절 12행, 1급 원천 [tech-stack-combination.md](../spec/sources/tech-stack-combination.md)

**바뀐 것(2026-11-02 21:10 KST).** 온프렘 클러스터는 Ubuntu VM 위 RKE2(CIS)입니다(ADR 0010). 아래의 `k3s` overlay는 `rke2` overlay로 바뀝니다. 랜딩 존 원칙은 그대로입니다.

## 맥락

스펙은 목적지를 온프렘 하나로 두고, 출발지 관리형 서비스의 대응을 AWS에서 온프렘으로 가는 한 방향 표로 둡니다(§7.3). 고객 클러스터마다 무엇을 설치하는지 정한 공통 묶음이 없습니다. 그런데 §4는 설계가 목적지와 무관하다고 적습니다. 1급 원천은 "모든 환경에 같은 도구를 설치하는 것은 목표가 아님"이라고 적습니다.

## 선택지

| 안 | 장점 | 단점 |
|---|---|---|
| A. 목적지마다 따로 구성(스펙의 한 방향 번역) | 처음 만들 것이 적음 | 방향마다 이전 설계가 따로. 첫 이전 뒤 다시 옮길 때 또 번역 |
| B. 같은 도구 · 같은 판을 모든 클러스터에 설치 | 모양이 완전히 같음 | 클라우드 위에 자체 저장소를 올려야 함. 1급 원천 원칙과 어긋남 |
| C. 같은 계약 · 같은 경험의 랜딩 존, 엔진은 공급자 overlay에서 고름 | 앱 매니페스트 · 이름 · 통제 시험 · 화면이 같음. 첫 이전 뒤에는 번역 없이 다시 옮김 | overlay 셋을 유지해야 함. 엔진이 다른 칸(객체 저장)은 행동 시험으로 동등성을 증명해야 함 |

## 결정

C를 고릅니다. 플랫폼이 만드는 모든 고객 클러스터에 같은 묶음을 같은 이름, 같은 정책으로 둡니다. 데이터베이스(CNPG + PgBouncer), 객체 · 블록 저장(같은 S3 계약, StorageClass `bravo-db` · `bravo-general`), 비밀(ESO만, 값은 플랫폼 OpenBao, ADR 0007), 네트워크(Cilium + Hubble, Gateway API, ADR 0008), 정책(Kyverno, OPA), 배포(Argo CD), 연결(`cf-edge`, `bravo-link`), 관측(OTel Collector 에이전트)입니다. 클라우드 차이는 overlay(`k3s`, `eks`, `gke`) 한 층에만 둡니다(`deploy/landing-zone/`).

클라우드 관리형 S3 · GCS · Secrets Manager · RDS는 목적지 구성요소가 아니라 첫 이전 때 번역할 출발지입니다. 예외는 객체 저장 엔진(ADR 0005)과 제안 D-24(관리형 PostgreSQL 끝점)입니다. 데모 출발지 `source-aws`는 바뀌지 않습니다. 앱이 S3 · Secrets Manager · KMS를 직접 부르는 상태가 S2의 재료이기 때문입니다(§8.10).

## 결과

- 쉬워지는 것: 컷오버 엔진, 첨부 복사, 비밀 이전이 방향과 상관없이 같습니다. 고객 입력은 대상 종류 · 이름 · 지역 · 크기뿐입니다.
- 어려워지는 것: overlay 셋과 판 고정 파일(`deploy/landing-zone/versions.env`)을 함께 관리합니다. 온프렘 K3s는 server 1대라 HA가 아닙니다.
- 2026-11-02 20:49 KST 상태: `k3s` overlay 설치기(`deploy/landing-zone/install.sh`)는 cilium · base · images · cnpg · secrets · app · gateway 단계를 갖고, 온프렘에서 앱 · CNPG · PgBouncer · `cloudflared`가 Running이었습니다. Kyverno, ESO, RGW 설치 단계는 아직 없습니다. `gke` overlay는 매니페스트가 있고 설치는 확인하지 못했습니다. `eks` overlay는 아직 없습니다(설치기 주석).
- 되돌리려면: overlay를 목적지별 구성으로 다시 나누면 됩니다. 앱 쪽 계약은 그대로 둡니다.
