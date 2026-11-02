# fix-bucket-policy(준비 중)

> 준비 중인 옛 템플릿입니다. 이 지시문의 도구 설명(ConfigMap apply, 모델의 `plan_propose`)은 지금 도구 계층과 맞지 않습니다. 어떤 역할도 kubectl을 받지 않고, 제출은 워커가 합니다. 리허설은 `governance` task를 씁니다.

## 목표

The source-parity gate blocked the move: on the target, the chat attachments bucket can be read without logging in. On the source (AWS S3), anonymous access is denied and only the `chat-app` principal may read and write objects. Propose the equivalent Rook-Ceph RGW bucket policy (`target.type = rgw_bucket_policy`) as one ConfigMap.

## 할 수 있는 것

- `inventory_describe`: the collected analysis for this plan.
- `repo_read`: read `source/s3-bucket-policy.json` and `app/policy.json`.
- `rehearsal_exec`: structured operations in your rehearsal namespace only (ConfigMap get/describe/apply/diff, files under `/app`).
- `policy_evaluate`: check a proposal before you submit it.
- `plan_propose`: submit exactly one proposal. The ConfigMap's `data["policy.json"]` must equal the `proposal` object.

## 할 수 없는 것

- Delete anything, touch other namespaces, create workloads, read Secrets, reach the network.
- See or change the tests that decide the result.

## 예산

The Trial has a deadline and a cost cap. Prefer reading the two files, one `policy_evaluate`, then `plan_propose`.

## 멈춤

Stop after `plan_propose` is accepted. If you cannot produce a valid proposal, stop without submitting.
