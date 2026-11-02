# rke2-traefik-crd 끄기 (운영 두 RKE2 클러스터)

RKE2에 들어 있는 Traefik CRD 차트를 `.skip` 파일로 멈췄습니다. 2026-11-03 00:07–00:11 KST에 운영 `bravo-onprem`과 `bravo-platform-rke2`에서 처리했습니다. Gateway API CRD, Gateway, HTTPRoute는 그대로이고, 공개 주소의 응답도 그대로입니다.

## 문제

- RKE2 v1.36.5+rke2r1은 `disable: [rke2-traefik]`이어도 `rke2-traefik-crd` 차트를 설치합니다.
- 이 차트는 랜딩 존이 먼저 넣은 Gateway API CRD를 자기 것으로 가져가려다 실패합니다. 예: `backendtlspolicies.gateway.networking.k8s.io`의 Helm 소유 표시가 없음.
- 그래서 `kube-system/helm-install-rke2-traefik-crd` Pod가 실패를 되풀이했습니다. `bravo-onprem`에서는 00:07 기준 재시작 37회였습니다.

## 처리 방법

RKE2 자동 배포 디렉터리 `/var/lib/rancher/rke2/server/manifests/`에 `<이름>.yaml.skip` 파일을 두면, RKE2는 그 매니페스트를 적용하지 않습니다.

- 새 노드: 공통 cloud-init 부트스트랩이 서버에서 RKE2를 시작하기 전에 `rke2-traefik-crd.yaml.skip`을 만듭니다(`infra/modules/cluster/onprem/cloud-init`). 새 VM에서 이 차트가 뜨지 않음을 확인했습니다(`../s1-cis-refusal/07-fix-skip.log`).
- 운영 노드: 파일 이름을 먼저 확인했습니다. `bravo-onprem` 서버에는 `rke2-traefik-crd.yaml`만 있고 `rke2-traefik.yaml`은 없습니다. 플랫폼 노드에는 둘 다 없습니다. 그래서 `rke2-traefik-crd.yaml.skip` 하나만 만들었습니다.
- `.skip` 파일은 이미 적용된 객체를 지우지 않습니다(00:08:20 확인). 그래서 남은 HelmChart를 지웠습니다.

## 클러스터별 기록

| 클러스터 | 처리 전(00:07:26) | 한 일 | 처리 뒤(00:09:53) |
|---|---|---|---|
| `bravo-onprem` | 매니페스트 `rke2-traefik-crd.yaml`, HelmChart · Addon · Job 있음, Pod `Error` 재시작 37회 | 00:07:50 서버 VM에 `.skip` 생성. 00:08:20 HelmChart 삭제 | HelmChart · Job · Pod 없음. 60초 뒤에도 다시 생기지 않음 |
| `bravo-platform-rke2` | 매니페스트 · HelmChart · Addon · Job · Pod 모두 없음 | 00:09:44 SSM으로 `.skip` 생성. 지울 것 없음 | 같음 |

- `bravo-onprem`의 HelmChart를 지우기 전에 Helm 릴리스 기록이 없음을 확인했습니다. 지우자 helm-controller가 `helm-delete-rke2-traefik-crd` Job을 돌렸습니다. 이 Job은 3초 만에 끝났습니다(15:08:20–23Z). 지울 릴리스가 없었으므로 지운 자원은 없습니다. 아래 비교로 확인했습니다.
- `bravo-onprem`의 Addon `rke2-traefik-crd`는 남아 있습니다. 매니페스트를 기록할 뿐이고 `.skip` 때문에 다시 적용되지 않습니다.
- 플랫폼 노드는 이미 처리돼 있었습니다. drop-in `30-platform.yaml`에 `disable+: [rke2-traefik-crd]`가 있습니다. `rke2-server`는 2026-11-02 22:12:09 KST에 마지막으로 시작했고, 매니페스트 파일 시각도 같습니다. 그때 `disable`이 차트를 지운 것으로 추정합니다. 팀장이 본 재시작 35회는 그 전 상태로 보입니다(추정). `.skip`은 새 노드와 공통 cloud-init에 맞추려고 더했습니다.

## 그대로인 것 (처리 전과 뒤 비교)

`state.sh`가 두 번 찍은 값을 `diff`로 비교했습니다. 바뀐 줄은 `.skip` 파일, 그리고 `bravo-onprem`의 HelmChart · Job · Pod 줄뿐입니다.

- Gateway API CRD 10개: 두 클러스터 모두 uid, resourceVersion, 생성 시각이 같습니다.
- `bravo-onprem`: Gateway `bravo-edge/bravo-gateway`(Programmed True), HTTPRoute `chat/chat`. uid, resourceVersion, generation이 같습니다.
- `bravo-platform-rke2`: Gateway `bravo-edge/bravo-gateway`(Programmed True), HTTPRoute `bravo-control/platform-front`, `bravo-observe/grafana`. uid, resourceVersion, generation이 같습니다.
- 공개 주소의 응답 코드가 같습니다.

| 주소 | 처리 전 | 처리 뒤 |
|---|---|---|
| `https://b-free.mangowhois.ai/` | 302 | 302 |
| `https://b-free.mangowhois.ai/healthz` | 302 | 302 |
| `https://b-free-chat.mangowhois.ai/api/config` | 200 | 200 |
| `https://b-free-chat-dst.mangowhois.ai/api/config` | 200 | 200 |
| `https://b-free-chat-src.mangowhois.ai/api/config` | 200 | 200 |

`b-free.mangowhois.ai`의 302는 Cloudflare Access 로그인(`*.cloudflareaccess.com/cdn-cgi/access/login/b-free.mangowhois.ai`)으로 보내는 응답입니다.

## 파일

| 파일 | 내용 |
|---|---|
| `state.sh` | 읽기만 하는 기록 스크립트. 매니페스트 디렉터리, Traefik 객체, Helm 릴리스 기록, Gateway API CRD와 객체, 공개 주소 |
| `01-before.txt` | 처리 전(00:07:26 KST) |
| `02-onprem.log` | `bravo-onprem`: `.skip` 생성, HelmChart 삭제, 삭제 Job 이벤트, 60초 뒤 확인 |
| `03-platform.log` | `bravo-platform-rke2`: drop-in의 `disable`, `.skip` 생성(SSM), 지울 것 없음 |
| `04-after.txt` | 처리 뒤(00:09:53 KST) |
