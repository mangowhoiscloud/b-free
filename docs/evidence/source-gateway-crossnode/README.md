# 출발지 Gateway 503 (노드 사이) 원인과 고침 (2026-11-03 04:12–04:44 KST)

## 증상

출발지 앱 노드가 2대가 된 뒤(03:47) `https://b-free-chat-src.mangowhois.ai`에서 응답의 약 3%가 503 `upstream connect error or disconnect/reset before headers. reset reason: connection timeout`이었습니다(조정자 04:12 측정 5/200, L1a의 SHIFT가 10%에서 되돌림, `b8a5404`). `hubble observe --verdict DROPPED`에는 아무것도 없었습니다.

환경: Cilium 1.20.4, aws-cni chaining, native routing, legacy host routing, kube-proxy replacement, Gateway API(Cilium Envoy, 외부 Envoy DaemonSet). 앱 Pod는 `ip-10-10-11-11`(cloudflared 2개와 같은 노드)과 `ip-10-10-11-107`에 하나씩 있습니다.

## 측정

| 확인 | 결과 |
|---|---|
| Envoy 클러스터 통계(`cilium-dbg envoy admin clusters`, 11번 노드 Envoy) | 다른 노드 Pod `10.10.11.169`: `cx_connect_fail` 3171 / `cx_total` 3517. 같은 노드 Pod `10.10.11.25`: 0 / 4206. 107번 노드 Envoy도 다른 노드 Pod로는 77 / 77 실패 |
| 같은 노드의 호스트 소켓에서 직접 연결(hostNetwork 진단 Pod, 11번 → `10.10.11.169:8000`) | 200 / 200 성공. 노드 사이 망 자체는 정상 |
| Envoy가 연결을 시도하는 동안의 소켓(`ss state syn-sent`, 11번 노드) | 멈춘 SYN의 출발 주소가 모두 `10.0.1.134`(`diag-envoy-syn-sent-sources.txt`). Cilium cluster-pool(`10.0.0.0/8`, 노드마다 `/24`)에서 받은 이 노드의 Gateway "ingress" IP. 성공한 연결은 노드 IP `10.10.11.11`에서 나감 |
| 107번 노드에 실제로 도착한 SYN(`cilium-dbg monitor`) | 연결 시도 27개 중 11개만 도착. 도착한 것에는 Pod가 SYN-ACK를 보냄 |
| Cilium 드롭 · BPF 드롭 카운터 · 커널 rp_filter | 늘지 않음. 11번 노드의 두 번째 ENI(ens6)로도 나가지 않음 |
| `trafficDistribution: PreferSameNode`(서비스 시험, 곧 되돌림) | EndpointSlice hint는 생기지만 Cilium Envoy의 엔드포인트 목록은 그대로 둘 다. 해결책 아님 |
| 임시 MASQUERADE `-s 10.0.1.0/24 -d 10.10.0.0/16 -o ens5`(11번 노드, 진단 뒤 지움) | 새 노드 간 연결 12개 중 실패 0, 요청 1500 / 1500 200 |

## 원인

Gateway Envoy는 백엔드 연결을 노드의 Cilium "ingress" IP(`10.0.<n>.x`, cluster-pool)에서 엽니다. aws-cni chaining에서는 Pod와 노드만 VPC 주소를 갖고, cluster-pool 대역은 VPC가 모릅니다. 같은 노드의 백엔드에는 BPF가 직접 넘겨 주므로 문제가 없습니다. 다른 노드로 가는 SYN은 ENI가 갖지 않은 출발 주소로 나가므로 VPC가 버립니다(EC2 source/destination check, 그 대역의 VPC 경로 없음). Cilium 밖에서 버려지므로 Hubble · 드롭 카운터에 보이지 않습니다. 노드 IP로 나간 일부 연결은 성공했고, Envoy가 그 연결을 재사용하는 동안은 오류가 없다가 새 연결이 필요할 때 503이 났습니다. 노드가 1대일 때는 백엔드가 늘 같은 노드여서 드러나지 않았습니다.

## 고침

`deploy/landing-zone/overlays/eks/cilium-values.yaml`: `enableIPv4Masquerade: true`, `ipv4NativeRoutingCIDR: 10.0.0.0/16`, `bpf.masquerade: false`. Cilium이 노드마다 아래 규칙을 넣습니다(`apply.txt`).

```
-A CILIUM_POST_nat -s 10.0.1.0/24 ! -d 10.0.0.0/16 ! -o cilium_+ -m comment --comment "cilium masquerade non-cluster" -j MASQUERADE
```

- 출발 주소가 그 노드의 cluster-pool `/24`인 패킷만 노드 IP로 바뀝니다. Pod는 VPC 주소(aws-cni)라 이 규칙에 걸리지 않습니다. Pod 트래픽의 SNAT는 지금처럼 aws-cni(AWS-SNAT-CHAIN)가 맡습니다.
- 적용: `helm upgrade cilium cilium/cilium --version 1.20.4 -f cilium-values.yaml`(04:32). 바뀐 설정은 `enable-ipv4-masquerade`, `enable-bpf-masquerade`, `ipv4-native-routing-cidr` 세 개뿐입니다(`helm template` 비교). 차트가 설정 변경으로 에이전트를 다시 띄우지 않으므로, 노드마다 하나씩 에이전트 Pod를 지워 다시 띄웠습니다(04:33). 진단용 임시 규칙은 지웠습니다.
- `chat-app-from-gateway`의 `remote-node` 허용(커밋 `e25808e`)은 그대로 필요합니다. 다른 노드에서 오는 Envoy 연결은 이제 노드 IP에서 오고, Cilium은 이를 `remote-node`로 봅니다.
- 범위: 출발지 EKS만. 온프렘 RKE2와 GKE는 aws-cni chaining이 아니어서 이 경로가 아닙니다. 온프렘은 확인하지 않았습니다.

## 확인(고친 뒤)

| 시험 | 결과 |
|---|---|
| 순서대로 600건 `GET /api/config`(운영자 Mac, 04:33:44–04:40:15) | **600 / 600 200**. 두 Pod가 305 / 295건씩 답함(다른 노드 Pod 포함). `sequential-600.txt` |
| 60초 부하 쓰기 50 + 읽기 150(플랫폼 Job, 04:42:35, [폴더](20261103-044235-gateway-burst-60s-50w150r/)) | **12,000건 모두 성공, 오류 시도 0**. 쓰기 p50 / p95 / p99 39.6 / 140.7 / 236.9 ms, 읽기 38.3 / 134.4 / 222.2 ms. NFR-2 OK. 그동안 Envoy의 새 노드 간 연결 65개, `cx_connect_fail` 0, `rq_error` 0(`envoy-*-burst2.txt`) |
| 60초 쓰기 50 + 읽기 250(목표 초과, 04:40:26, [폴더](20261103-044026-gateway-burst-60s/)) | Envoy 새 노드 간 연결 949개, `cx_connect_fail` 0, `rq_error` 0. 5xx 0. 다만 앱이 읽기 250/s를 감당하지 못해 시간 초과 1,009건과 `client_saturated`가 남(용량 문제이고 Gateway 문제 아님) |

Argo 상태: `argo-state.txt`(04:44).
