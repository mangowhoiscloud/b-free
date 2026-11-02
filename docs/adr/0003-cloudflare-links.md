# 0003. 환경 사이 연결을 Cloudflare Tunnel + Access 링크로

- 상태: 제안(설계안 2026-11-02, 11/3 회의 O1에서 채택 여부를 정함). Tailscale 제외만 팀장 판단
- 날짜: 2026-11-02
- 정한 사람: 제안 인프라 · CI, 데이터 · 컷오버 담당. 최종 결정은 팀장
- 스펙 위치: §6 S3 시작 조건, §7.5, §8.6, §8.8, T1, T2, T8, T30, D-16
- 근거 문서: `docs/spec/DEVIATIONS.md` D-16, `docs/architecture/02-network.md`(14절 열린 결정 O1–O13), `deploy/CLOUD-E2E-CONTRACT.md` 6 · 7절, 설계안 `research/platform-research/network-design-2026-11-02.md`(저장소 밖), `docs/spec/TECH-STACK-SPEC.md` 2.2절

## 맥락

스펙은 복제, 제어, 첨부 복사를 Tailscale ACL로 잇고, 플랫폼에서 고객 환경으로는 VPC 피어링이나 Tailscale을 씁니다(§7.5). 팀장(류지환)은 Tailscale의 신뢰성을 문제로 들어 다른 연결을 고르게 했습니다. 제약은 넷입니다.
- 어느 환경에도 인바운드 포트를 열지 않습니다.
- 호텔 NAT 뒤 온프렘 장비에서도 돌아야 합니다.
- 장기 키를 만들지 않습니다(NFR-6). 키는 플랫폼 금고 하나에만 둡니다(D-26).
- 사용자 진입은 이미 Cloudflare Tunnel(7844 아웃바운드)을 씁니다(§8.7).

## 선택지

| 안 | 판정(설계안) | 이유 |
|---|---|---|
| A. Tailscale(스펙) | 제외(팀장 판단) | 신뢰성 우려 |
| B. Cloudflare Tunnel + Access `access tcp` 링크 | 주 경로 | 7844 아웃바운드만. 사용자 진입과 같은 포트. Zero Trust Free, Tunnel 추가 비용 없음 |
| C. AWS Site-to-Site VPN(GCP HA VPN) | 데이터면 폴백 | 지연이 가장 작다고 봄(추정). UDP 500 · 4500 필요, 호텔에서 막히면 끊김 |
| D. Cloudflare Mesh(WARP Connector) | 승격 경로 | L3라 장기 TCP 세션 보존. 노드 설치, 최소 3 vCPU |
| E. WireGuard 직접 | 제외 | AWS 쪽 UDP 수신 지점이 필요해 인바운드 0과 부딪힘 |

## 결정(제안)

B를 제안합니다. 커넥터 하나(`cloudflared`)가 제어면(K8s API, PgBouncer 관리, DB 제어)과 데이터면(논리 복제 5432, 첨부 복사)을 함께 나릅니다. Access가 링크마다 다른 서비스 토큰을 Service Auth 정책으로 확인합니다. 폴백 규칙과 판정 시각(N5 · N11 · N16, 11/4 18:00)은 D-16 "결정 시점과 선택 규칙"에만 둡니다.

## 결과

- 쉬워지는 것: 온프렘이 새로 요구하는 포트가 없습니다. 피어링, 노드 공인 IP, Tailscale ACL이 빠집니다.
- 어려워지는 것: 사용자 진입, 운영자 로그인, 환경 사이 연결이 모두 Cloudflare를 지납니다. Cloudflare가 멈추면 새 컷오버를 시작하지 않고, 진행 중이면 SAFE_HOLD입니다(`02-network.md` 14절). 서비스 토큰은 7일짜리 비밀로 남아 NFR-6과 완전히 맞지 않습니다. 커넥터 재접속 때 5432 장기 세션이 끊길 수 있습니다.
- 2026-11-02 20:50 KST 상태: Zero Trust가 꺼져 있어 B는 아직 쓸 수 없습니다. 지금 연결은 "임시 링크(D-16 전)"인 플랫폼 노트북의 `kubectl port-forward`입니다(`deploy/cloud/link.sh`, 계약 7절). 사용자 진입 Tunnel은 있습니다. `b-free-chat-dst`는 HTTP 200, `b-free-chat-src`는 커넥터가 없어 530이었습니다. 앞단 가중치 전환은 코드만 있고 실행 기록이 없습니다(감사 B21).
- 되돌리려면: 링크 이름(`src-db`, `dst-db` 등)을 그대로 두고 링크 구현만 C나 D로 바꿉니다. Executor 토폴로지의 `conninfo`만 바뀝니다.
