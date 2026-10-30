<div align="center">

<img src="docs/images/logo.png" alt="Nexus" width="100%">

**A provider-agnostic agent harness for your terminal and browser.**<br>
One agentic loop, any model, real approvals, and a visible, ordered context timeline.

</div>

Nexus 0.10 is built from the provider wire up: a Python Core (`nexus-core/`) that owns sessions, tools and the agent loop,
and two clients that speak only its HTTP API: a Ratatui terminal client (`nexus-tui/`) and an NSH web and desktop client
(`nexus-nsh/`). What the model receives is what you see, in order, and clipping is announced.

## Install

```sh
curl -LsSf https://github.com/nottherealsanta/nexus/releases/latest/download/install.sh | sh
```

The installer is attached to each GitHub release, so this works once 0.10.0 is published. Until then, install from a checkout;
it needs `uv`, Rust (`cargo`), Node and `nsh`:

```sh
git clone https://github.com/nottherealsanta/nexus && cd nexus
./install.sh               # ./install.sh --uninstall removes it; your data in ~/.nexus is never touched
```

Nexus keeps its state in `~/.nexus`. If you used an older Nexus, run `nexus migrate` once; it moves the old directory aside and
never deletes it. Old Nexus (0.7.x) is on the `nexus01` branch and the `v0.7.2` tag.

## Quick start

```sh
nexus login codex          # or: claude, copilot, grok, opencode, openai, groq, nvidia, ollama
nexus                      # the terminal client
nexus web                  # the browser client
nexus doctor               # check the install
```

The `claude` provider drives your own Claude Code through the Agent SDK. Install Claude
Code and sign in with `claude auth login` first; Nexus does not ship it.

## Documentation

[docs/README.md](docs/README.md) indexes the docs, which describe the current code: providers, auth, the agent loop, tools, the
HTTP API and the clients. [PLAN.md](PLAN.md) lists what is left for 0.10.0 and [TODO.md](TODO.md) what comes after.
Working on Nexus itself? Start with [AGENTS.md](AGENTS.md).
