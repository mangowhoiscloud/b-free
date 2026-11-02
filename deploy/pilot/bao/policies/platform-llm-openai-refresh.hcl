# Agent worker, openai_agents in ChatGPT-account mode (13 6, D-26, ADR 0009; demo only): write a refreshed session
# back to the same path. The worker uses KV v2 check-and-set (options.cas) and first takes a 60 s refresh lease, so
# two workers never spend one refresh token twice. Same AppRole llm-openai (secret_id 60 s / 1 use, token 2 m).
path "secret/data/envs/platform/llm-openai" { capabilities = ["update"] }
