# Rehearsal service (platform/rehearsal, D-26): read-only, the four server-side tools and the B7 verification Job.
# db_inspect: collector_ro per environment. db_rehearse: the synthetic DB. evidence_read: the
# platform DB as bravo_rehearsal_ro. Nothing else: no Cloudflare, no chat-admin, no chat-db.
path "secret/data/envs/+/collector-ro" { capabilities = ["read"] }
path "secret/data/platform/rehearsal-synthetic-db" { capabilities = ["read"] }
path "secret/data/platform/rehearsal-evidence-db" { capabilities = ["read"] }
# Behavioural verification Job (B7, platform/rehearsal behavioral.py): the rehearsal test bucket owner's key on the
# destination RGW. The service hands it only to the Job's Secret in verify-<plan>-<n>-job (deleted after the Job).
path "secret/data/envs/+/rehearsal-s3" { capabilities = ["read"] }
# Model credential delivery to the agent worker (13 6). The service never reads the credential: it checks
# presence from KV metadata (/readyz) and mints one secret_id (60 s, 1 use) per Trial for the provider's
# AppRole (llm-claude, llm-openai; policies platform-llm-*.hcl), which the worker uses to read it once.
path "secret/metadata/envs/platform/llm-claude" { capabilities = ["read"] }
path "secret/metadata/envs/platform/llm-openai" { capabilities = ["read"] }
path "auth/approle/role/llm-claude/role-id" { capabilities = ["read"] }
path "auth/approle/role/llm-claude/secret-id" { capabilities = ["update"] }
path "auth/approle/role/llm-openai/role-id" { capabilities = ["read"] }
path "auth/approle/role/llm-openai/secret-id" { capabilities = ["update"] }
# App verification Job (platform/rehearsal behavioral_app.py, S2 code fix): one secret_id (120 s, 4 uses) per Trial for
# AppRole rehearsal-eso-<env>, handed to a namespaced SecretStore in verify-<plan>-<role>-<n>-app; ESO on the
# destination logs in with it and reads only secret/envs/<env>/rehearsal-app-db (policy rehearsal-eso-<env>,
# rehearsal-bao.sh eso-setup). The service never reads that value itself. One pair of lines per destination: a "+"
# glob only matches a whole path segment and "rehearsal-eso-*" would also match the role itself (role update), so the
# names are explicit (measured 2026-11-03 03:00 KST: "rehearsal-eso-+" -> 403 on secret-id).
path "auth/approle/role/rehearsal-eso-target-onprem/role-id" { capabilities = ["read"] }
path "auth/approle/role/rehearsal-eso-target-onprem/secret-id" { capabilities = ["update"] }
