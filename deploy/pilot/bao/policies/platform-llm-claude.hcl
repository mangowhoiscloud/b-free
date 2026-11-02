# Agent worker, claude_subscription (13 6, D-26): read the lead's subscription OAuth token, nothing else.
# Used by AppRole llm-claude: secret_id 60 s / 1 use, token 2 m. The worker reads once, then revokes its token.
path "secret/data/envs/platform/llm-claude" { capabilities = ["read"] }
