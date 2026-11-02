# envs/source-aws

출발지(blue) 환경입니다. EKS 1.36과 채팅 앱이 기대는 AWS 관리형 서비스를 만듭니다. 이 폴더는 apply할 수 있게 준비했습니다. apply는 팀 승인 뒤에만 합니다(`infra/README.md` 규칙).

## 무엇을 만드나

- VPC `10.10.0.0/16`, 사설 서브넷 둘, Regional NAT 하나(`modules/network/aws`).
- EKS 1.36. API는 비공개 엔드포인트만 엽니다. 관리형 노드 그룹 `app` 하나를 첫 AZ에 둡니다(`modules/cluster/eks`).
- 둘째 서브넷은 EKS의 "서로 다른 AZ 서브넷 둘" 요건 때문에 둡니다. 출발지의 AZ 장애 내성은 주장하지 않습니다.
- 앱 데이터 KMS 키 하나. S3 첨부와 Secrets Manager 비밀을 암호화합니다.
- 첨부 S3 버킷. 공개 접근 차단 넷을 모두 켭니다. SSE-KMS와 버킷 키, 버전 관리, `BucketOwnerEnforced`를 겁니다.
- Secrets Manager 비밀 `<name>/chat/db-app`. 그릇만 만듭니다.
- EKS Pod Identity 역할 둘: `chat/chat-app`, `external-secrets/external-secrets`.
- ECR 저장소 둘(`ecr.tf`): `<name>/bravo-demo`, `<name>/bravo-pgbouncer`. 태그 변경 불가, 푸시 때 스캔, 전용 KMS CMK. 배포는 digest로만 합니다.
- landing zone 묶음의 Argo CD Application 파일(`gitops/`). 클러스터에 적용하지 않습니다.

## 첨부 버킷 정책

| `demo_weak_bucket_policy` | 정책 |
|---|---|
| `false`(기본) | TLS가 아닌 요청을 거부합니다. 객체 읽기와 쓰기는 앱 역할과 `bucket_admin_principal_arns`만 합니다. |
| `true`(S1 데모) | TLS 강제가 없습니다. 계정 전체에 `s3:*`를 엽니다. |

- 약한 정책도 공개 정책이 아닙니다. 그래서 공개 접근 차단에 걸리지 않습니다.
- S1에서 게이트가 이 차이(TLS 강제 없음, 넓은 주체)를 잡는 것이 목적입니다.

## DB 비밀번호 값을 넣는 방법

Terraform은 비밀 값을 만들지 않습니다. 값이 상태 파일에 남기 때문입니다(NFR-6).

- 클러스터 안에서 CNPG가 앱 역할(`app_writer`) 비밀번호를 만듭니다(Secret `chat/chat-db-app`).
- `deploy/landing-zone/overlays/eks/install.sh`(단계 `schema`)가 그 값을 파이프로 `aws secretsmanager put-secret-value --secret-string file:///dev/stdin`에 넘깁니다. 파일이나 인자에 남지 않습니다.
- 같은 단계가 값을 플랫폼 OpenBao `secret/envs/source-aws/chat-db`(필드 `app_writer`)에도 파이프로 씁니다(Executor 계약 §9).
- ESO는 이 비밀을 읽어 PgBouncer 인증 Secret에 합칩니다(`ExternalSecret chat-pgbouncer-auth`).
- ESO 역할의 `PutSecretValue` 권한은 지금 쓰지 않습니다. ESO 2.14 `PushSecret`은 비밀에 `managed-by=external-secrets` 태그와 `DeleteResourcePolicy` 권한을 요구하기 때문입니다(ESO 소스 `providers/v1/aws/secretsmanager`).

앱은 이 비밀을 SDK로 직접 읽습니다. 이 의존성은 S2의 재료로 일부러 둔 것입니다(스펙 §8.10).

## 권한

| 서비스 계정 | 받는 권한 |
|---|---|
| `chat/chat-app` | 첨부 버킷 `ListBucket`, 객체 `GetObject` · `PutObject`, 비밀 하나 `GetSecretValue`, 앱 키 `Decrypt` · `GenerateDataKey`(S3와 Secrets Manager 경유만) |
| `external-secrets/external-secrets` | `<name>/` 아래 비밀 읽기, DB 비밀 하나에 `PutSecretValue`, 앱 키(Secrets Manager 경유만) |
| `kube-system/ebs-csi-controller-sa` | `modules/cluster/eks`가 만듦 |

## 입력

고객이 고르는 값은 `name`, `region`, `size`(small, medium)입니다. 나머지는 `gitops_repo_url`, `tags`, `demo_weak_bucket_policy`, `bucket_admin_principal_arns`입니다. 예시는 `terraform.tfvars.example`입니다.

## 명령

검증만 합니다:

```
terraform -chdir=infra/envs/source-aws init -backend=false
terraform -chdir=infra/envs/source-aws validate
```

승인 뒤 올리기(S3 백엔드 설정 파일이 필요합니다):

```
terraform -chdir=infra/envs/source-aws init -backend-config=backend.hcl
terraform -chdir=infra/envs/source-aws apply
```

- 비밀은 데모에서 다시 올리기 쉽게 바로 지웁니다(`recovery_window_in_days = 0`). 운영은 30일로 둡니다.
- KMS 키는 삭제 대기 7일이 남습니다. 대기 중 과금은 없습니다(`01-environments.md` 6절).
