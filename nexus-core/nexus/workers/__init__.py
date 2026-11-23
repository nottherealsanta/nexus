"""CLI worker transports used by `workers.agent.ExternalAgent`.

`acp.py` is the stdio JSON-RPC transport, `antigravity.py` holds the ACP worker and the legacy
stream-json worker kept for old sessions and fixtures, and `claude_code.py` is the PTY worker.
The workers own their tools and permissions. Importing this package does not discover a CLI, read
credentials, or change any provider or session behavior.
"""
