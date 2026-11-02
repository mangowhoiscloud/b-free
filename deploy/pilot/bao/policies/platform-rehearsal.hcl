# Rehearsal service (platform/rehearsal, D-26): read-only, the four server-side tools only.
# db_inspect: collector_ro per environment. db_rehearse: the synthetic DB. evidence_read: the
# platform DB as bravo_rehearsal_ro. Nothing else: no Cloudflare, no chat-admin, no chat-db.
path "secret/data/envs/+/collector-ro" { capabilities = ["read"] }
path "secret/data/platform/rehearsal-synthetic-db" { capabilities = ["read"] }
path "secret/data/platform/rehearsal-evidence-db" { capabilities = ["read"] }
# Model credential delivery to the agent worker (13 6). The service never reads the credential: it checks
# presence from KV metadata (/readyz) and mints one secret_id (60 s, 1 use) per Trial for the provider's
# AppRole (llm-claude, llm-openai; policies platform-llm-*.hcl), which the worker uses to read it once.
path "secret/metadata/envs/platform/llm-claude" { capabilities = ["read"] }
path "secret/metadata/envs/platform/llm-openai" { capabilities = ["read"] }
path "auth/approle/role/llm-claude/role-id" { capabilities = ["read"] }
path "auth/approle/role/llm-claude/secret-id" { capabilities = ["update"] }
path "auth/approle/role/llm-openai/role-id" { capabilities = ["read"] }
path "auth/approle/role/llm-openai/secret-id" { capabilities = ["update"] }
