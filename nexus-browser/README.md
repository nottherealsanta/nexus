# Nexus Browser

The default browser MCP is the upstream **browser-use** local stdio server.
This directory contains launch configuration, not a custom browser server.
Core offers it as the default MCP server (PLAN.md G, `nexus-core/nexus/mcp/config.py`
`DEFAULT_SERVERS`), switched off until the user enables it in Settings; then it is
written into `config.toml`. `mcp.json` here is the same entry in the portable
format (`nexus config import-mcp` reads it).

Upstream launch command:

```sh
uvx --from 'browser-use[cli]==0.13.14' browser-use --mcp
```

Reference: https://github.com/browser-use/browser-use/blob/main/skills/open-source/references/integrations.md

Pinned to 0.13.14. Verified on 2026-10-29 through Nexus's MCP manager: it launched,
discovered 16 tools, and `browser_navigate` and `browser_get_state` fetched
example.com. Screenshots and the optional LLM-backed tools were not exercised.
Core owns lazy startup, shutdown, health, session isolation and error reporting.
Browser-use is configured by default, subject to each agent's tool restrictions.

Optional LLM-backed browser-use tools may require their own provider credentials;
Nexus subscription sign-ins are not assumed to supply those. Keep credentials
in environment/private Core configuration, never in this tracked file. Browser
profiles, downloads and screenshots belong under `$NEXUS10_HOME`, not here and
never under old Nexus's state directory.
