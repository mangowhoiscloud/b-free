# kube-bench `rke2-cis-1.9` on `bravo-onprem` (데모 목적지, 2026-11-03 02:31–02:34 KST)

리뷰 P17. kube-bench v0.16.0(이미지 digest 고정)을 게이트 러너(`python -m bravo_gate kube-bench`)로 데모 목적지에서 처음 돌렸습니다. 서버 노드는 모든 대상(master, node, controlplane, etcd, policies), 에이전트 노드는 node 대상입니다. `rke2-cis-1.9`는 RKE2 v1.36에 근사치입니다.

## 러너를 고친 것

이전 실행(플랫폼, 버리는 VM)의 Job은 SA 토큰을 붙이지 않았습니다. 그래서 정책 점검 5.1.x의 `kubectl`이 `localhost:8080` 연결 거부로 실패했고, 5.1.1 · 5.1.3 · 5.1.5 · 5.1.6 FAIL에는 도구 오류가 섞여 있었습니다. 이번 러너는 get/list만 하는 ClusterRole의 토큰을 붙이고, `/etc/passwd` · `/etc/group` · `/var/log`도 읽기 전용으로 붙입니다(`platform/gate/src/bravo_gate/kube_bench/runner.py`).

## 결과

| 실행 | 노드 | PASS | FAIL | WARN | INFO |
|---|---|---|---|---|---|
| `onprem-server-20261102T173152Z.json`(고치기 전) | server | 77 | 4 | 42 | 7 |
| `onprem-server-20261102T173437Z.json`(고친 뒤) | server | 78 | 3 | 42 | 7 |
| `onprem-agent-20261102T173251Z.json` | agent | 16 | 1 | 2 | 5 |

5.1.1(cluster-admin)은 토큰이 붙자 PASS입니다. 앞선 FAIL은 도구 오류였습니다.

## FAIL마다 판단 (`judgements.json`)

| 항목 | 판단 | 이유 |
|---|---|---|
| 4.3.1 kube-proxy metrics가 localhost에 묶임 | 해당 없음 | kube-proxy가 없습니다. Cilium `kube-proxy-replacement: true`. 감사 명령 `ps -fC proxy`가 프로세스를 못 찾아 FAIL로 셉니다. 두 노드 같음 |
| 5.1.3 Role · ClusterRole의 와일드카드 | 예외 | `ceph-csi-clientprofilereplication-admin-role`(Helm `rook-ceph`, 차트 ceph-csi-operator-1.1.0)이 `clientprofilereplications`에 verbs `*`. 묶인 바인딩 0건, aggregate 라벨 없음(02:33 확인). 고쳐도 Helm이 되돌립니다 |
| 5.1.5 기본 ServiceAccount를 쓰지 않음 | **고침** | 11개 네임스페이스의 기본 SA가 토큰 자동 마운트 미설정이었습니다. 공통 랜딩 존 `install.sh` 단계 `sa_automount`(지금 있는 네임스페이스 패치) + Kyverno MutatingPolicy `bravo-default-sa-no-automount`(이후 네임스페이스, 새 네임스페이스로 시험함). 재실행 PASS. 기본 SA 토큰에 기대던 Pod는 0개(61개 확인) |
| 5.1.6 필요한 곳에만 SA 토큰 | 일부 고침 + 예외 | 37 → 26. 고친 11개는 Pod는 이미 `false`인데 SA가 미설정이라 kube-bench 스크립트가 FAIL로 센 것(SA를 `false`로). 남은 26개는 API를 쓰는 Pod(ESO, Kyverno, CNPG 운영자 · 인스턴스, local-path, Rook 운영자 · mgr · CSI, osd-prepare, 스캐너 자신)와 Rook이 사양을 만드는 Ceph 데몬(mon, osd, rgw, exporter). Ceph 데몬은 바꿀 설정이 없고, 잘못되면 데모 저장소가 멈춰 데모 전에는 두지 않습니다 |

kube-bench 5.1.6 스크립트는 "SA 미설정 + Pod false"를 FAIL로 셉니다. 같은 항목의 설명은 그 조합을 준수로 적습니다. 스크립트와 설명이 다릅니다(v0.16.0 `rke2-cis-1.9` 정의).

## 게이트에서

이 요약은 게이트 보고서의 `cis_benchmark`(`judged: false`)로 붙습니다. 판정에는 넣지 않습니다(DEVIATIONS D-29 #6). 만들기: `python3 -I docs/evidence/kube-bench/cis-summary.py > deploy/platform/control/config/gate-cis-evidence.json`. 만료는 수집 7일 뒤입니다.

## 출발지 EKS · GKE (같은 날, 판단 안 함)

같은 러너로 출발지 EKS(`eks-1.8.0`, `../source-20261103/`)와 GKE(`gke-1.9.0`, `../gke-20261103/`)도 돌렸습니다. FAIL은 EKS 12, GKE 10이고 판단하지 않았습니다(`unjudged`). GKE의 5.1.1(`gcloud: not found`) · 5.6.7(`INGRESS_FORBIDDEN`)은 도구 오류입니다.

## 다시 돌리기

```bash
cd platform/gate
uv run --frozen python -m bravo_gate kube-bench --context bravo-onprem --platform rke2 --out <file>
uv run --frozen python -m bravo_gate kube-bench --context bravo-onprem --platform rke2 --node bravo-onprem-agent --targets node --out <file>
deploy/landing-zone/install.sh rke2 bravo-onprem sa_automount     # 5.1.5 고침(멱등)
```
