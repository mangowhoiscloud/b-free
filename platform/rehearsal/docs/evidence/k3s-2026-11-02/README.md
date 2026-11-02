# K3s 시기 기록(2026-11-02, 컨텍스트 bravo-onprem)

목적지 클러스터가 Ubuntu VM + RKE2(CIS)로 바뀌기 전, Lima의 K3s v1.36.5(Cilium + Hubble, Kyverno, PSA restricted)에서 실제로 돈 기록입니다. 데모 기록은 RKE2에서 같은 실행을 다시 해서 남깁니다.

| 무엇 | 결과 | 증거 |
|---|---|---|
| `BaseEnvironment` 9개 메서드(시작 · exec · 올리기 · 내려받기 · 멈춤) | 통과(`tests/test_harbor.py::test_every_environment_method_on_real_cluster`, 91.6초) | 아래 격리 기록 |
| 격리 시험(T3): 외부 송신 | `1.1.1.1:443`, `169.254.169.254:80` 모두 `TimeoutError: timed out`(약 3.1초), 막힘으로 기록 | `isolation-t-1793621514975174000.json` |
| 격리 시험: privileged Pod 승인 | 거부 | 같은 파일 |
| 기본 거부 NetworkPolicy | 클러스터 정책(Kyverno `bravo-default-deny`)이 먼저 만든 `default-deny`를 같은 내용인지 확인하고 받음 | 같은 파일의 `baseline_notes` |
| 분리 검증기(Harbor `Verifier`, 검증 NS) · Harbor #2551 | 샌드박스에 미리 심은 reward 1은 무시, 나쁜 제안서는 reward 0, 샌드박스에서 검증기로 가는 업로드 거부, rehearsal-exec 계정은 검증 Pod를 읽지 못함(V1). 마지막 판정 단계는 검증기 이미지 결속(아래) 때문에 INFRA_ERROR였고, 고친 뒤 다시 돌리기 전에 클러스터가 내려감 | `tests/test_harbor.py::test_separate_verifier_ignores_preseeded_reward_on_real_cluster` |
| 에이전트 실제 리허설(governance, Claude Agent SDK) | 실행 못 함. 시작 때 `cluster_unreachable`(K3s를 RKE2로 바꾸는 중 컨텍스트가 지워짐). 모델 호출 없음 | `../rehearsals.tsv`의 "K3s 시기 기록" 행 |

이 시기에 알게 된 사실:

- `docker save` 뒤 `k3s ctr images import --digests`로 넣은 이미지는, 컨테이너 상태의 imageID로 `import-<날짜>@sha256:<tar 색인>`을 먼저 보여 줍니다. 그래서 검증기 이미지 결속은 노드의 이미지 기록(`Node.status.images[].names`)에 고정 digest가 있는지로 확인하게 고쳤습니다(`harbor_run.accept`, 단위 시험 있음).
- import 참조를 지우면 CRI 캐시가 지운 참조를 계속 찾아 `CreateContainerError`가 납니다. 같은 tar로 `--digests` import를 다시 하면 풀립니다.
- `../rehearsals.tsv`의 `k8s_409` 행은 Claude Agent SDK로 바꾸기 전 Codex 경로(`codex_chatgpt`)로 한 시도이고, 같은 이름의 `default-deny`를 Kyverno가 먼저 만든 탓에 멈췄습니다. 고친 뒤로는 나지 않았습니다. 그 실행은 에이전트를 띄우기 전에 멈췄습니다(모델 호출 없음).
