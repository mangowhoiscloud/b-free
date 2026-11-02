# T9 secret migration (spec r2 T9, CLOUD-E2E-CONTRACT 8, D-26): one-time copy of the source app's
# Secrets Manager values into secret/envs/target-onprem/chat/*. Used only by scripts/migrate-secrets.sh
# through a 5-minute AppRole token. Read is allowed only to compare digests after the write.
# No list, no delete, no other path.
path "secret/data/envs/target-onprem/chat/*" { capabilities = ["create", "update", "read"] }
path "secret/metadata/envs/target-onprem/chat/*" { capabilities = ["read"] }
