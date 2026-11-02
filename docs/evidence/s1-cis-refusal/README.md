# S1 예외 4a: CIS 시작 거부와 재설치 (실행 기록)

버리는 Lima VM 하나에서 스펙 S1 예외 4a와 S1 인수 조건을 실제로 돌린 기록입니다. 2026-11-02 23:37–23:52 KST(4a), 23:54–23:58 KST(인수 조건)에 실행했습니다. 2026-11-03 00:06–00:11 KST에는 부트스트랩을 고친 뒤 4a 거부를 다시 돌렸습니다. 이제 거부가 5초 안에 실패로 드러납니다. 데모 각본 `docs/demo/SCRIPT.md` 4절 S1 순서 2, 2a가 이 폴더를 가리킵니다.

- 스펙 4a: "RKE2 CIS 프로필이 조건 미충족으로 시작을 거부한다 → 공통 cloud-init의 조건(sysctl, etcd 사용자)을 고치고 재설치한다."
- 인수 조건: "Given 빈 VM만 있는 목적지, When 구축을 실행하면, Then RKE2가 CIS 프로필로 뜨고 실행 기록이 남는다."

## 결과

| 항목 | 판정 | 근거 |
|---|---|---|
| 4a 거부 | PASS | 조건을 뺀 공통 cloud-init으로 설치하자 `rke2-server`가 `profile: cis` 검사에서 멈췄습니다. 23:37:47 첫 거부, 10분 동안 117회 거부(`01-refuse.log`) |
| 4a 거부를 바로 알림 | PASS | 부트스트랩을 고친 뒤(11/3) 공통 경로가 5초 만에 exit 1로 끝나고 거부 원문을 찍습니다(`06-refuse-failfast.log`) |
| 4a 고침과 재설치 | PASS | 같은 VM에서 `rke2-uninstall.sh` 뒤 공통 cloud-init 기본값으로 다시 설치했습니다. 92초 뒤 API 준비, 그 뒤 거부 0회(`02-fix.log`) |
| 인수 조건 | PASS | RKE2를 한 번도 깔지 않은 새 VM에 공통 cloud-init 기본값으로 구축했습니다. 75초 뒤 API 준비, `profile: cis`, 거부 0회, Cilium 뒤 노드 `Ready`(`04-accept.log`). 이 로그가 실행 기록입니다 |
| kube-bench | 기록 | `rke2-cis-1.9`: PASS 76, FAIL 5, WARN 42, INFO 7. 플랫폼 RKE2 실행(`../kube-bench/platform-rke2-20261102T124326Z.json`)과 항목별 판정이 모두 같습니다(확인함) |

## 실행 환경

- VM: `bravo-cis-demo`. Ubuntu 24.04.5 LTS arm64, 커널 6.8.0-147. 이미지 · vmType(vz) · 네트워크(user-v2)는 `bravo-onprem` VM과 같습니다. Ceph 데이터 디스크는 없습니다.
- 운영 중인 `bravo-onprem` VM과 `~/.kube/config`는 건드리지 않았습니다. kubeconfig는 임시 파일만 썼습니다.
- RKE2 v1.36.5+rke2r1. 핀은 `infra/envs/target-onprem/main.tf`에서 읽었습니다. tarball SHA-256 검사는 두 번 모두 `OK`였습니다.
- 실행이 끝난 뒤 VM을 지웠습니다(`03-clean.log` 23:52:38, `05-clean.log`).

## 조건을 빼는 방법

공통 cloud-init 모듈(`infra/modules/cluster/onprem/cloud-init`)에 변수 `cis_preconditions`를 더했습니다. 기본값은 `true`입니다. `false`는 이 데모에서만 씁니다. `false`이면 CIS sysctl 네 줄과 etcd 사용자 · 그룹 생성이 빠집니다. 손으로 고친 사본은 쓰지 않았습니다.

- 기본값으로 만든 문서는 변수를 더하기 전과 바이트 단위로 같습니다. sha256 `f21de113…`(server), `e0e7dd6c…`(agent)로 확인했습니다. 그래서 `bravo-onprem`과 플랫폼 EC2의 user-data는 바뀌지 않습니다.
- 시험: `infra/modules/cluster/onprem/cloud-init/tests/cis_preconditions.tftest.hcl`(`terraform test`, 2개 통과).
- 고친 내용(조건을 뺀 문서에서 기본 문서로): [cloud-init-fix.diff](cloud-init-fix.diff).

## 흐름 (2026-11-02, KST)

| 시각 | 일 | 기록 |
|---|---|---|
| 23:37:42 | 빈 VM 준비. `rke2` 없음, etcd 사용자 · 그룹 없음, sysctl 네 값 모두 0 | `01-refuse.log` |
| 23:37:43 | 공통 경로 `rke2-node.sh server`로 설치 시작(`cis_preconditions = false`) | `01-refuse.log` |
| 23:37:47 | 첫 거부. sysctl 세 값이 CIS 값과 다름 | `01-refuse.log` |
| 23:47:57 | `rke2-node.sh`가 614초 뒤 `API not ready`로 끝남(exit 1) | `01-refuse.log` |
| 23:48:28 | 진단(손으로 한 일, 고치는 경로 아님). sysctl만 맞추자 다음 거부는 etcd 사용자 · 그룹. 바꾼 값은 되돌림 | `01-refuse.log` |
| 23:48:49 | 고침. 공통 cloud-init 기본값, `rke2-uninstall.sh`, 같은 공통 경로로 재설치 | `02-fix.log` |
| 23:50:22 | API 준비(재설치 시작 뒤 92초). `NRestarts=0` | `02-fix.log` |
| 23:52:07 | 랜딩 존 Cilium 1.20.4 뒤 노드 `Ready`. kube-bench 실행 | `02-fix.log` |
| 23:52:38 | VM 삭제 | `03-clean.log` |
| 23:54:14 | 인수 조건. 같은 이름으로 새 빈 VM. `rke2` 없음, etcd 사용자 없음 | `04-accept.log` |
| 23:55:43 | 공통 cloud-init 기본값으로 API 준비(설치 시작 뒤 75초). `NRestarts=0`, 거부 0회 | `04-accept.log` |
| 23:58 전 | 랜딩 존 Cilium 뒤 노드 `Ready` | `04-accept.log` |
| 23:58:12 | VM 삭제 | `05-clean.log` |

2026-11-03, 부트스트랩을 고친 뒤입니다.

| 시각 | 일 | 기록 |
|---|---|---|
| 00:07:01 | 새 빈 VM에 조건을 뺀 문서로 설치 시작 | `06-refuse-failfast.log` |
| 00:07:06 | 부트스트랩이 `rke2-server did not start`와 거부 원문을 찍고 exit 1. `rke2-node.sh`도 5초 만에 exit 1 | `06-refuse-failfast.log` |
| 00:07:27 | 고침과 재설치(공통 cloud-init 기본값). 76초 뒤 API 준비 | `07-fix-skip.log` |
| 00:11 전 | Cilium 뒤 노드 `Ready`. `rke2-traefik-crd` HelmChart 없음, Gateway API CRD 10개 | `07-fix-skip.log` |
| 00:11:04 | 돌고 있는 노드에서 공통 경로를 다시 실행. `MainPID`, 시작 시각, `NRestarts`가 그대로 | `08-rerun.log` |
| 00:11:05 | VM 삭제 | `09-clean.log` |

## 거부 원문 (journal)

sysctl이 맞지 않을 때입니다.

```
2026-11-02T23:37:47+09:00 lima-bravo-cis-demo rke2[1953]: time="2026-11-02T23:37:47+09:00" level=fatal msg="invalid kernel parameter value kernel.panic=0 - expected 10\ninvalid kernel parameter value kernel.panic_on_oops=0 - expected 1\ninvalid kernel parameter value vm.overcommit_memory=0 - expected 1\n"
```

sysctl만 맞추고 etcd 사용자가 없을 때입니다(진단).

```
2026-11-02T23:48:33+09:00 lima-bravo-cis-demo rke2[5632]: time="2026-11-02T23:48:33+09:00" level=fatal msg="missing required: user: unknown user etcd\nmissing required: group: unknown group etcd\n"
```

RKE2는 두 조건을 차례로 검사합니다. sysctl이 틀리면 etcd 사용자 오류는 로그에 나오지 않습니다(확인함).

## CIS 프로필이 적용된 근거 (재설치 뒤)

- `/etc/rancher/rke2/config.yaml`: `profile: cis`, `protect-kernel-defaults: true`, `secrets-encryption: true`, PSA 설정, 감사 정책.
- etcd 프로세스가 `etcd` 사용자로 돕니다. kube-apiserver와 kubelet은 `root`입니다.
- kube-apiserver 인자: `--admission-control-config-file`(PSA), `--audit-policy-file`, `--audit-log-path`, `--encryption-provider-config`, `--anonymous-auth=false`, `--profiling=false`.
- sysctl 네 값: `vm.panic_on_oom=0`, `vm.overcommit_memory=1`, `kernel.panic=10`, `kernel.panic_on_oops=1`.
- kube-bench FAIL 5개는 4.3.1, 5.1.1, 5.1.3, 5.1.5, 5.1.6입니다. 플랫폼 RKE2 실행과 같은 항목입니다. kube-bench v0.16.0의 최신 RKE2 기준이 `rke2-cis-1.9`라서 v1.36에는 근사치입니다.

## 실행으로 드러난 문제

두 문제 모두 팀장 결정에 따라 고쳤습니다(2026-11-03 00시대 KST).

| # | 문제 | 측정 | 처리 |
|---|---|---|---|
| 1 | 공통 부트스트랩이 거부를 바로 알리지 못합니다 | Ubuntu 24.04(systemd 255)에서 `systemctl enable --now`는 시작이 실패해도 exit 0입니다(23:48:24). `systemctl start`만 부르면 exit 1입니다(23:3x 손으로 확인). 그래서 `rke2-node.sh`가 API를 기다리다 614초 뒤에 실패합니다 | 고침. 부트스트랩이 `enable`과 `start`를 나눠 부르고, 시작이 실패하면 journal의 `level=fatal` 줄을 찍고 exit 1로 끝납니다. 다시 돌린 기록: `06-refuse-failfast.log`. 돌고 있는 노드에는 영향이 없습니다. 플랫폼 EC2는 `ignore_changes = [ami, user_data]`라 plan이 "No changes"이고, 온프렘은 Lima VM 교체가 없습니다(`10-terraform-plans.txt`). 온프렘 apply가 공통 경로를 다시 돌려도 같은 프로세스가 유지됩니다(`08-rerun.log`) |
| 2 | `helm-install-rke2-traefik-crd` Job이 실패를 되풀이합니다 | 랜딩 존이 먼저 넣은 Gateway API CRD(`backendtlspolicies.gateway.networking.k8s.io`)를 RKE2 traefik CRD 차트가 가져가려다 실패합니다. 운영 `bravo-onprem`에서도 같은 Pod가 `CrashLoopBackOff`입니다(23:5x 관측, 재시작 34회) | 고침. 공통 cloud-init이 서버에 `rke2-traefik-crd.yaml.skip`을 만듭니다. 새 노드에서 이 차트가 뜨지 않음을 확인했습니다(`07-fix-skip.log`). 운영 두 클러스터 처리 기록: `../traefik-crd-skip/README.md` |

## 다시 돌리는 법

필요한 것: `limactl`, `terraform` 1.17, `kubectl`, `helm`, `jq`, `yq`. 노트북 포트 36443을 씁니다.

```
cd bravo-platform
D=docs/evidence/s1-cis-refusal
$D/run.sh refuse > $D/01-refuse.log 2>&1   # 약 11분. 거부와 진단
$D/run.sh fix    > $D/02-fix.log 2>&1      # 약 4분. 고침, 재설치, Cilium, kube-bench
$D/run.sh clean  > $D/03-clean.log 2>&1    # VM 삭제
$D/run.sh accept > $D/04-accept.log 2>&1   # 인수 조건만. 새 빈 VM
$D/run.sh clean  > $D/05-clean.log 2>&1
# 11/3: 고친 부트스트랩으로 다시(kube-bench 생략)
export BRAVO_CIS_DEMO_KUBE_BENCH=0
$D/run.sh refuse > $D/06-refuse-failfast.log 2>&1   # 약 1분
$D/run.sh fix    > $D/07-fix-skip.log 2>&1
$D/run.sh rerun  > $D/08-rerun.log 2>&1             # 돌고 있는 노드에서 공통 경로 다시 실행
$D/run.sh clean  > $D/09-clean.log 2>&1
```

## 기록 손질과 한계

- 로그의 로컬 경로(저장소 루트, 임시 폴더)만 줄였습니다. 임시 폴더는 `$WORK/`로 적었습니다.
- 비밀 값은 없습니다. 에이전트 토큰은 VM 밖으로 나오지 않습니다. 출력은 토큰 모양 값을 가리는 필터를 거쳤습니다.
- `01-refuse.log`의 결과 단계가 한 번 끊겼습니다(`grep -m1`이 journal 파이프를 끊어 exit 255). `run.sh`를 고친 뒤 같은 VM에서 결과 단계만 다시 돌렸습니다(23:48:24).
- 23:26–23:36에 첫 시도가 있었습니다. 실행 쪽에서 건 300초 시간 제한이 공통 경로를 잘랐습니다. 그 VM은 지웠고 기록은 남기지 않았습니다. 문제 1은 그때 처음 보였습니다.
- `01`–`03` 로그를 만든 뒤 `run.sh`가 명령 줄을 stderr로 찍도록 고쳤습니다. 그래서 `01`–`03`에는 `limactl create`, `start`, `delete` 명령 줄이 빠져 있습니다.
- 단일 노드 VM입니다. 2노드 `bravo-onprem`의 구축 기록은 이 폴더에 없습니다.

## 파일

| 파일 | 내용 |
|---|---|
| `run.sh` | 실행 스크립트(`refuse`, `refuse-result`, `fix`, `accept`, `rerun`, `clean`) |
| `01-refuse.log` | 빈 VM, 조건을 뺀 설치, 거부, 진단 |
| `02-fix.log` | 고친 문서 diff, 재설치, CIS 근거, Cilium, 노드 `Ready`, kube-bench |
| `03-clean.log`, `05-clean.log` | VM 삭제 |
| `04-accept.log` | 인수 조건: 새 빈 VM에 공통 cloud-init 기본값으로 구축 |
| `06-refuse-failfast.log` | 고친 부트스트랩으로 4a 거부. 5초 만에 실패로 드러남 |
| `07-fix-skip.log` | 고침과 재설치, `rke2-traefik-crd` HelmChart 없음, 노드 `Ready` |
| `08-rerun.log` | 돌고 있는 노드에서 공통 경로를 다시 실행. 같은 프로세스 유지 |
| `09-clean.log` | VM 삭제 |
| `10-terraform-plans.txt` | 바뀐 user-data에 대한 온프렘 · 플랫폼 `terraform plan`(적용하지 않음) |
| `cloud-init-fix.diff` | 조건을 뺀 문서와 기본 문서의 차이 |
| `kube-bench-bravo-cis-demo-20261102T145207Z.json` | kube-bench 원본(JSON) |
