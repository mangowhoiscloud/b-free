# 고객 클러스터 관리 Tunnel(D-16)

서비스를 내놓는 고객 클러스터(출발지 EKS, 온프렘 RKE2)의 `cf-edge`에서 도는 관리 Tunnel 커넥터입니다. 링크 호스트 `t1-<env>-{k8s,db,pgb}`를 클러스터 안 원본으로 보냅니다(`docs/architecture/02-network.md` 4.2). 라우트는 Cloudflare 원격 구성이 아니라 이 디렉터리의 설정 파일입니다(`config_src = "local"`).

**Zero Trust를 켜기 전에는 적용하지 않습니다.** 순서는 `../d16/README.md`와 같습니다. 토큰 Secret `cf-mgmt-tunnel`(키 `token`)은 플랫폼 OpenBao `secret/envs/<env>/cf-mgmt-tunnel`에서 파이프로 넣습니다.

```
kubectl --context <env> apply -k deploy/platform/link/customer-mgmt-tunnel/overlays/<env>
```

사용자 진입 Tunnel(`t1-*-web`, CLOUD-E2E-CONTRACT.md 3절 `cf-edge/cloudflared`)과는 다른 Deployment입니다. 진입과 관리 경로를 섞지 않습니다.
