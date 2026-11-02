# Agent worker, openai_agents (13 6, D-26): read the OpenAI credential (api_key, or the demo ChatGPT session), nothing else.
# Used by AppRole llm-openai: secret_id 60 s / 1 use, token 2 m. The worker reads once, then revokes its token.
path "secret/data/envs/platform/llm-openai" { capabilities = ["read"] }
