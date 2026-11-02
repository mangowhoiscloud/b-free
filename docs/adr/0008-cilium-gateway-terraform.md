# 0008. 모든 환경에 Cilium + Hubble, 환경 안 진입은 Gateway API, IaC는 Terraform 1.17

- 상태: 채택
- 날짜: 2026-11-02
- 정한 사람: 류지환(팀장). Cilium과 Gateway API는 1급 원천 「기술 스택(툴) 조합」을 읽고 정함, Terraform은 같은 날 따로 정함
- 스펙 위치: §7.4, §7.5, §8.7, §8.8, FR-8, NFR-7, NET-01(§8.5)
- 근거 문서: `docs/spec/TECH-STACK-SPEC.md` 1절 · 2.1절 · 2.2절, `docs/spec/ALIGNMENT-2026-11-02.md` 1절 3 · 6행과 2절, `docs/architecture/09-cni-csi.md`, 1급 원천 [tech-stack-combination.md](../spec/sources/tech-stack-combination.md), `deploy/landing-zone/versions.env`

**바뀐 것(2026-11-02 21:10 KST).** 온프렘과 플랫폼은 RKE2입니다(ADR 0010). 아래의 "K3s"와 "플랫폼 EKS"는 RKE2로 읽습니다. Cilium + Hubble, Gateway API, Terraform 결정은 그대로이고, RKE2에 Cilium을 붙이는 방법은 ADR 0010의 제안입니다.

## 맥락

세 가지를 함께 정했습니다.
- **CNI.** 앞 설계는 환경마다 기본 CNI(EKS VPC CNI, K3s flannel, GKE Dataplane V2)를 썼습니다(옛 `09-cni-csi.md`). 그러면 정책 집행 엔진과 흐름 기록이 환경마다 달라 NET-01 증거가 같은 뜻이 아닙니다. 1급 원천은 Cilium/Hubble을 보안 관측 · 격리의 도구로 지정합니다.
- **진입.** 스펙 FR-8은 환경 사이 가중치를 Cloudflare LB로 둡니다. 1급 원천은 Envoy 기반 안정 진입점과 환경별 Gateway API 연결을 둡니다. 환경 안에서 터널이 무엇을 가리키는지는 정해지지 않았습니다.
- **IaC.** 앞 문서는 Terraform 1.5.7(마지막 MPL 판)에서 돌게 썼습니다(옛 `infra/README.md`).

## 선택지

| 주제 | 안 | 판정 | 이유 |
|---|---|---|---|
| CNI | 환경별 기본 CNI | 제외 | 관측 · 집행이 환경마다 다름 |
| CNI | Cilium + Hubble을 모든 환경에(EKS는 VPC CNI chaining, K3s는 VXLAN, GKE는 Dataplane V2) | 채택 | 같은 집행과 같은 흐름 기록. GKE Dataplane V2가 이미 Cilium 기반 |
| CNI | Calico | 제외 | RKE2와 함께 뺌 |
| 진입 | 앱 Service를 터널이 바로 가리킴 | 폴백 | 만들 수 없을 때만(계약 3절) |
| 진입 | Gateway API(Envoy, Cilium 구현). `cloudflared`는 Gateway로 보냄 | 채택 | 환경 안 진입점이 세 환경에서 같은 API. 인바운드 0이라 ALB 없음 |
| IaC | Terraform 1.5.7(MPL) | 제외 | 최신 판의 기능과 보안 수정을 못 받음 |
| IaC | OpenTofu | 제외 | 팀장 결정. 업계 표준과 수상작 대부분이 Terraform |
| IaC | Terraform 1.17.1 | 채택 | 공식 릴리스 + SHA256 확인 설치(`tools/toolchain/install-terraform.sh`) |

## 결정

- 세 환경 모두 Cilium 1.20.4 + Hubble입니다. EKS는 VPC CNI chaining이고 VPC CNI 정책 에이전트는 끕니다(`enableNetworkPolicy=false`). K3s는 VXLAN이고 flannel을 끕니다. GKE는 Dataplane V2와 관리형 Hubble입니다. Tetragon은 P1입니다.
- 정책은 표준 NetworkPolicy와 Kyverno `GeneratingPolicy`로 겁니다. Cilium 고유 정책은 쓰지 않습니다(`09-cni-csi.md` 2절).
- 환경 사이 가중치는 Cloudflare LB(FR-8), 환경 안 진입점은 Gateway API `bravo-gateway`(ns `bravo-edge`)입니다. 플랫폼 진입(`b-free`)도 같은 모양입니다(`infra/envs/edge/platform.tf`).
- 모든 Terraform 루트는 `required_version = "~> 1.17.0"`입니다.

## 결과

- 쉬워지는 것: Hubble 흐름(`POLICY_DENIED`)이 세 환경에서 같은 NET-01 증거가 됩니다. S2의 "네임스페이스 밖 접속 차단" 장면이 같은 화면으로 보입니다.
- 어려워지는 것: EKS에서는 Cilium이 깔리기 전까지 NetworkPolicy를 집행하는 주체가 없습니다(VPC CNI 정책 에이전트를 끄기 때문). 2026-11-02 20:25 KST의 `bravo-source`가 그 상태였고, 20:49에는 Cilium Pod가 Running이었습니다. 설치 뒤 대조 시험(V4 · V5)으로 확인해야 합니다.
- 어려워지는 것: Istio ambient(제안 D-15)와 기능이 겹칩니다. 11/4 판정 전에 D-15의 대안 비교를 고칩니다. Gateway API CRD 판이 K3s 설치기(v1.7.0)와 EKS 값 파일 주석(v1.6.3)에서 다릅니다.
- 앞단 가중치 전환은 코드만 있고 실행 기록이 없습니다(감사 B21). LB pool도 아직 없습니다.
- 되돌리려면: CNI는 overlay의 Cilium 값만 바꾸고 표준 NetworkPolicy는 그대로 둡니다. Terraform은 `required_version`만 바꿉니다.
