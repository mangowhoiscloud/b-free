# envs/platform-aws

플랫폼(중앙 제어) 환경입니다. EKS 1.36을 세 AZ에 둡니다. 검증만 하고 apply하지 않습니다.

- VPC `10.20.0.0/16`, 사설 서브넷 셋, Regional NAT 하나.
- 노드 풀 `core`는 AZ마다 1대입니다. `agents`는 셋째 AZ에 두고 `pool=agents:NoSchedule` taint를 겁니다.
- CNI는 VPC CNI(IPAM) 위에 Cilium chaining입니다. Cilium과 Hubble은 landing zone 묶음이 깝니다(`modules/cluster/eks` README).
- Istio ambient는 이 클러스터에만 둡니다. Cilium과 같이 쓸 때의 설정은 `docs/architecture/07-runtime-mesh-observability.md` 4절에 있습니다.
- 입력은 `name`, `region`, `size`, `gitops_repo_url`, `tags`입니다.

```
terraform -chdir=infra/envs/platform-aws init -backend=false
terraform -chdir=infra/envs/platform-aws validate
```
