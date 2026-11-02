"""init 격리 검사(claude_engine.check_init). 실측 init 모양(CLI 2.1.316, 2026-11-02)을 본뜬 값."""
import pytest

from bravo_agents.claude_engine import check_init
from bravo_agents.errors import AgentRunError

GRANTED = ("inventory_describe", "repo_read")
OK = {"tools": ["mcp__platform__inventory_describe", "mcp__platform__repo_read", "StructuredOutput"],
      "mcp_servers": [{"name": "platform", "status": "connected", "source": "dynamic"}],
      "plugins": [{"name": "cc-plugin-agents-md", "path": "builtin", "source": "cc-plugin-agents-md@builtin"}],
      "apiKeySource": "none", "skills": ["deep-research"], "agents": ["Explore"]}


def test_clean_init_passes():
    check_init(OK, GRANTED, "claude_subscription")


@pytest.mark.parametrize("patch,code", [
    ({"tools": OK["tools"] + ["Bash"]}, "unexpected_tools"),
    ({"tools": OK["tools"] + ["mcp__platform__plan_propose"]}, "unexpected_tools"),
    ({"tools": OK["tools"] + ["mcp__exa__search"]}, "unexpected_tools"),
    ({"mcp_servers": OK["mcp_servers"] + [{"name": "exa", "status": "connected"}]}, "unexpected_mcp_servers"),
    ({"mcp_servers": []}, "unexpected_mcp_servers"),
    ({"mcp_servers": [{"name": "platform", "status": "failed"}]}, "mcp_unavailable"),
    ({"tools": ["mcp__platform__inventory_describe", "StructuredOutput"]}, "mcp_unavailable"),
    ({"plugins": [{"name": "ponytail", "path": "/Users/op/.claude/plugins/ponytail"}]}, "unexpected_plugins"),
    ({"apiKeySource": "ANTHROPIC_API_KEY"}, "unexpected_credentials"),
])
def test_any_drift_fails_closed(patch, code):
    with pytest.raises(AgentRunError) as e:
        check_init({**OK, **patch}, GRANTED, "claude_subscription")
    assert e.value.code == code and e.value.infra


def test_gateway_may_report_an_auth_source():
    check_init({**OK, "apiKeySource": "ANTHROPIC_AUTH_TOKEN"}, GRANTED, "anthropic_gateway")
