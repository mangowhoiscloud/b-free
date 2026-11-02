# deploy

클러스터 안 구성요소 배포 매니페스트입니다. 방식(Argo CD 또는 Actions + Helm)은 인프라 · CI 담당이 정합니다(§12). 제안은 Actions + Helm입니다(`docs/architecture/06-tech-stack.md` 6절). 이미지는 태그가 아니라 GHCR digest로 지정합니다.
