# 0004. RKE2를 빼고 플랫폼 · 출발지는 EKS, 온프렘은 K3s, GCP는 GKE. K8s는 세 환경 1.36

- 상태: 채택(클러스터 종류). K8s 1.36은 **제안**(실무 결정, 팀장 확인 대기)
- 날짜: 2026-11-02
- 정한 사람: 류지환(팀장) 결정, 회의 공유. K8s 1.36은 EKS 생성 결과에 맞춘 실무 결정
- 스펙 위치: FR-3, §4, §6 S1, §7.2, §7.3, §8.4, §8.5, §8.8, §9, T2, T5, T7, D-21(D-17을 대체)
- 근거 문서: `docs/spec/DEVIATIONS.md` D-21, `docs/spec/TECH-STACK-SPEC.md` 1절 · 2.1절 · 3절 7번, `docs/architecture/10-cluster-overview.md`, `docs/architecture/09-cni-csi.md` 3절

## 맥락

스펙은 플랫폼과 목적지를 모두 RKE2로 둡니다(플랫폼 EC2 1대 + RKE2, 온프렘 Ubuntu VM + RKE2 CIS). 리뷰에서 두 문제가 나왔습니다.
- 일본에서 RKE2를 공개적으로 채택한 사례를 찾지 못했습니다. 근거를 말할 수 없는 선택은 쓰지 않습니다(TECH-STACK-SPEC 0절).
- 고객은 엔터프라이즈지만 쿠버네티스 초보자입니다. 플랫폼이 제어면(etcd, 인증서, 업그레이드)을 운영하면 "몰라도 된다"를 지키기 어렵습니다.

심사위원이 아는 소프트뱅크 AGENTIC STAR는 관리형 EKS · AKS 위에 설치됩니다(REA 분석, D-21 이유 1).

## 선택지

| 안 | 장점 | 단점 |
|---|---|---|
| A. 스펙 그대로 RKE2(플랫폼 3-AZ HA는 D-17) | 배포판이 하나 | 채택 근거 약함. 제어면 운영 부담 |
| B. 플랫폼 · 출발지 EKS, 온프렘 K3s, GCP GKE | 관리형 우선. 심사위원이 아는 구성. 온프렘은 가벼운 CNCF 배포판 | 배포판이 셋이라 통제 비교(kube-bench)가 같은 뜻의 항목만 비교됨 |
| C. 온프렘 OpenShift | 일본 금융 사례 있음(MUFG ROSA, 라쿠텐은행) | VM 두 대에 무겁고 구독 필요 |

K8s minor 선택지는 1.35(앞 권고)와 1.36이었습니다. 1.35는 kube-bench 자동 판 대응과 GKE STABLE 채널이 장점이었습니다.

## 결정

B를 고릅니다. 보안 동등성은 배포판이 같아서가 아니라 같은 질문의 시험 결과로 증명합니다(§8.5 원칙). 게이트 규칙은 그대로입니다.

K8s minor는 세 환경 모두 1.36으로 둡니다(제안). 출발지 EKS `bravo-source`가 1.36으로 생성됐고(`aws eks describe-cluster`, 2026-11-02), EKS는 minor를 내릴 수 없기 때문입니다. 플랫폼 EKS `bravo-platform`(1.36), K3s v1.36.5+k3s1(`infra/envs/target-onprem/main.tf`), GKE 1.36(REGULAR 채널)이 이미 같은 판으로 떴습니다.

## 결과

- 쉬워지는 것: 플랫폼 제어면을 우리가 운영하지 않습니다. 고객 입력은 대상 종류 · 이름 · 지역 · 크기뿐입니다.
- 어려워지는 것: kube-bench는 1.36 자동 대응이 없어 `--benchmark`로 벤치마크 이름을 명시합니다(`04-governance.md` 1절). GKE는 1.36이 STABLE에 들어오는 2026-11-11 무렵까지 REGULAR 채널을 씁니다. EKS 제어면 요금이 클러스터마다 시간당 $0.10 듭니다(D-21 비용 표).
- 2026-11-02 20:49 KST 상태: EKS `bravo-source` ACTIVE(노드 1), EKS `bravo-platform` ACTIVE, K3s `bravo-onprem` 노드 2 Ready, GKE `bravo-target-gcp` RUNNING.
- 스펙 v0.2에서 위 스펙 위치의 RKE2 문구를 바꿉니다.
- 되돌리려면: 클러스터 모듈(`infra/modules/cluster/*`)만 바꿉니다. 랜딩 존과 앱 매니페스트는 클러스터 종류를 모릅니다(ADR 0006).
