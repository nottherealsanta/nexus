# Nexus Browser

The browser tools `browser_exec` and `browser_screenshot` run on upstream
**browser-use**, pinned (`nexus-core/nexus/browser.py`; see
[docs/tools.md](../docs/tools.md#browser-tools)). It is not an MCP server in Settings.
Core starts one stdio process per session at that session's first browser call.

Launch commands (Core runs them; `uvx` must be on `PATH`):

```sh
uvx --from 'browser-use[cli]==1.0.2' browser-use --mcp       # default: its own Chromium, temporary profile
uvx --from 'browser-use[cli]==1.0.2' browser-use --cli-mcp   # Settings > Browser > Use my Chrome profile
```

`--mcp` never attaches the user's Chrome (in 1.0.2 it is the deprecated name for the same
tools on browser-use's own Chromium; the deprecation note goes to the log). `--cli-mcp`
drives the user's Chrome once its remote debugging is on, and otherwise falls back to
its own Chromium, and may open `chrome://inspect` in the user's Chrome to ask for that.
That is why it is opt-in.

browser-use 1.0.2 also offers `browser_login` (Browser Use Cloud) and `browser_feedback`
(reports to its team). Nexus does not offer them to the model.

Pinned to 1.0.2 (upgraded from 0.13.14 on 2026-11-12). The tool definitions are Nexus's
own, in `nexus/browser.py`. To upgrade, change `PACKAGE` there and check the flags and
tool names still hold.

Exercised on 2026-11-13: a real model (codex/gpt-6.1-terra) used both tools on a live
site, and the default mode was checked to report "this server's own Chromium on a temp
profile, not your Chrome". Driving the user's Chrome (`use_my_chrome` on) was not
exercised.

What a machine needs: `uvx` and a Chrome, Chromium or Edge (or the Chromium
`browser-use install` downloads). `nexus doctor`, `GET /api/browser` and Settings >
Browser say whether both are there.

Logs: `~/.nexus/logs/browser/<session>.log`.
