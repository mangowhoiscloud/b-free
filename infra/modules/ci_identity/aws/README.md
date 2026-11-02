# ci_identity/aws

GitHub Actions가 장기 키 없이 AWS 역할을 받는 OIDC 신원입니다(T7, NFR-6).

- 신뢰: `token.actions.githubusercontent.com`의 토큰 가운데 `aud = sts.amazonaws.com`이고 `sub`가 `repo:<github_repository>:ref:<github_ref>`와 정확히 같은 것만. 와일드카드는 받지 않습니다.
- 권한: `ecr_repository_arns`에 적은 저장소에 이미지 push(`PutImage`와 계층 업로드)와 그 저장소의 읽기. 클러스터, S3, 비밀에는 권한이 없습니다. 배포는 각 클러스터의 Argo CD가 Git을 당겨 옵니다(D-16).
- OIDC 공급자는 계정당 URL 하나만 둘 수 있습니다. 이미 있으면 `create_oidc_provider = false`(기본)로 찾아 씁니다.

| 출력 | 뜻 |
|---|---|
| `deploy_identity` | 역할 ARN. 워크플로의 `role-to-assume` |
| `trusted_subject` | 신뢰하는 `sub` 값 |
| `oidc_provider_arn` | 공급자 ARN |
