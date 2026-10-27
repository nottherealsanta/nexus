# Nexus Browser

The default browser MCP is the upstream **browser-use** local stdio server.
This directory contains launch configuration, not a custom browser server.
Core MCP support and loading this default are planned in PLAN.md Step 11a;
`mcp.json` is a proposed Core configuration, not wired into the daemon yet.

Upstream launch command:

```sh
uvx --from 'browser-use[cli]' browser-use --mcp
```

Reference: https://github.com/browser-use/browser-use/blob/main/skills/open-source/references/integrations.md

Before integration lands, pin a tested browser-use version, document its browser
installation steps, and verify tool discovery, navigation and screenshots. The
command above is upstream's unpinned setup example and was not launched here.
Core owns lazy startup, shutdown, health, session isolation and error reporting.
Browser-use is configured by default, subject to each agent's tool restrictions.

Optional LLM-backed browser-use tools may require their own provider credentials;
Nexus subscription sign-ins are not assumed to supply those. Keep credentials
in environment/private Core configuration, never in this tracked file. Browser
profiles, downloads and screenshots belong under `$NEXUS10_HOME`, not here and
never under old Nexus's state directory.
