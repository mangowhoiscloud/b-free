# envs/target-gcp

두 번째 목적지(제안 D-01)입니다. GKE Autopilot 리전 클러스터를 만듭니다. 검증만 하고 apply하지 않습니다.

- K8s 1.36, 릴리스 채널 `REGULAR`. 1.36은 2026-11-11 무렵 Stable에 들어옵니다(GKE 출시 일정, 2026-11-02 확인).
- CNI는 Dataplane V2입니다. NetworkPolicy 집행과 관측을 GKE가 맡습니다.
- 첨부 객체 저장은 Cloud Storage 버킷입니다. 균일 버킷 접근, 공개 접근 방지(`enforced`), 버전 관리를 켭니다. 앱은 같은 S3 계약 환경 변수로 받습니다.
- landing zone 묶음은 gke overlay로 Argo CD Application 파일만 씁니다.
- 입력은 `project_id`, `name`, `region`, `size`, `gitops_repo_url`, `deletion_protection`, `labels`입니다.

```
terraform -chdir=infra/envs/target-gcp init -backend=false
terraform -chdir=infra/envs/target-gcp validate
```
