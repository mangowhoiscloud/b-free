# Rehearsal service (platform/rehearsal, D-26): read-only, the four server-side tools only.
# db_inspect: collector_ro per environment. db_rehearse: the synthetic DB. evidence_read: the
# platform DB as bravo_rehearsal_ro. Nothing else: no Cloudflare, no chat-admin, no chat-db.
path "secret/data/envs/+/collector-ro" { capabilities = ["read"] }
path "secret/data/platform/rehearsal-synthetic-db" { capabilities = ["read"] }
path "secret/data/platform/rehearsal-evidence-db" { capabilities = ["read"] }
