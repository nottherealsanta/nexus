# Antigravity worker startup troubleshooting

Nexus uses the installed `agy` executable, not the Gemini CLI. Check the executable
on the Nexus daemon's `PATH` with `agy --version` and `agy --help`.

## Headless launch

The worker launches:

```text
agy --input-format stream-json --output-format stream-json --model <id> --mode <mode> --print-timeout <duration>s
```

The provider's [headless CLI contract](https://antigravity.google/docs/cli/headless)
specifies that `--input-format stream-json` selects streaming operation; `--print`
and `-p` are not required (their command-line prompts are ignored in this mode).
The prompt is a newline-terminated `user` JSON event on stdin, never on
argv. Nexus keeps stdin open until agy's terminal `result` event and only then
closes it: the headless docs say the streaming session lasts until stdin closes,
so closing right after the prompt (the earlier behaviour) could end the session
before the turn finished, showing as "stream ended without a terminal result". Preflight checks the version and required streaming CLI flags/values before starting
a turn. Some agy releases (including the locally inspected 1.3.4) write help to
stderr, so Nexus collects both output streams for the bounded preflight probe.
A help-on-stderr release can appear to lack every required flag if only stdout
is inspected; this was the original preflight failure, not a need to add `--print`.
A missing flag error now names the missing capabilities. Do not work around a
missing capability by disabling permission checks.

## Empty answer after SUCCESS (soft-denied tool)

In headless mode agy cannot ask for approval: shell commands default to Ask and
are soft-denied unless agy's own `settings.json` has a scoped `permissions.allow`
rule (see the headless docs, "Permissions in headless mode"; workspace file reads
are auto-allowed). agy can then end with `status: SUCCESS` and an empty
`response`. Nexus never reports that as `completed`: with no answer text (neither
the `result.response` nor a finished `agent_response` step) the assignment ends
as `needs_attention`, naming the likely soft denial. Nexus does not add allow
rules or answer prompts, and passes `--dangerously-skip-permissions` only when the
user-level opt-in [below](#opting-in-to---dangerously-skip-permissions) is set; otherwise
edit agy's own settings with a narrowly scoped rule and re-run.

This applies only to Nexus-managed Antigravity workers started by the Nexus runtime
(they emit `external.started` events). An Antigravity subagent launched directly by
an outer coding harness's own subagent tool (for example `--mode auto` with no Nexus
`external.started` events) is outside Nexus code: `~/.nexus/config.toml` and
`PathGuard` do not apply to it. If it repeatedly denies `RunCommand` with a harness
label, Nexus config will not fix it; change that harness's own permission settings or
pick a different agent backend.

agy 1.3.6 makes the denial explicit, and Nexus surfaces all three signals:

- the tool step ends with `state: ERROR` and a `tool_info.error` ("user denied
  permission to run command …"). Nexus closes the tool row as an error on any
  ending state (`DONE`, `ERROR`, `CANCELED`), not only `DONE`, so a denied row no
  longer looks like it is still running;
- `result.denied_actions` (e.g. `{"action":"command","display_name":"RunCommand"}`)
  is named in the attention reason and in a "Denied by agy" footer row;
- on accounts where `--mode auto` is unavailable, agy prints `warning: --mode auto
  is not available for this account; continuing in the default mode.` on stderr
  and its `init.permission_mode` is `request-review`. Nexus adds a "Mode fallback"
  footer row and appends it to the attention reason. In that mode every shell
  command needs an allow rule (or a mode that works for the account).

## Opting in to `--dangerously-skip-permissions`

If you accept that agy will auto-approve **all** tool actions (shell, edits, MCP)
with no Nexus mediation, add to your user-level `~/.nexus/config.toml` (this affects only
Nexus-managed Antigravity workers, not external harness or tool integrations that launch
agy themselves):

```toml
[providers.antigravity]
kind = "antigravity"
worker_dangerously_skip_permissions = true
```

- It is off by default and is never read from a project file: putting the key in
  `nexus.toml`, `.nexus/nexus.toml` or `.agents/nexus.toml` fails the config load
  with "may only be set in the user-level config". Restart a long-running daemon
  after changing it.
- It also fixes the `--mode auto` fallback. agy's [modes docs](https://antigravity.google/docs/cli/modes/)
  list `default`, `accept-edits` and `plan` (not `auto`), and say shell commands are governed by
  tool permission rules or `--dangerously-skip-permissions` in every mode. So the bypass lets
  `RunCommand` run whichever mode agy picks. With the opt-in, a configured `worker_permission_mode`
  of `auto` (the default) is replaced by `accept-edits`, so agy is never asked for the mode that
  some accounts reject. An explicit `accept-edits` or `plan` is passed unchanged. Preflight then
  requires `accept-edits` in `agy --help`. Without the opt-in the default stays `auto`.
- Without it, an `init` with `permission_mode: always-proceed` (e.g. from agy's own
  global settings) is refused, with a hint naming this key.
- `agy --help` must list `--dangerously-skip-permissions`; otherwise the run is
  refused before launch ("lacks required headless flags").
- Results and the UI header show the bypass (`skip_permissions`, "Permissions:
  BYPASSED"). Nexus never edits agy's own settings for you.

## Settings inheritance and the Nexus child path

`providers.<name>.worker_inherits_agy_settings` defaults to `true`, including when
the provider section or option is omitted and for the `agy` alias. Explicit
`false` remains an opt-out and refuses the worker: agy loads its own settings,
MCP servers and plugins, outside Nexus PathGuard. It is not an isolated worker.
The same policy is used by direct workers and Nexus model children. Older
section-like callers that represent an omitted option as `None` also receive the
default; an explicit `false` is never overwritten.

If an omitted option still produces the old “set worker_inherits_agy_settings =
true” error after updating the source, restart the long-running Nexus daemon
and reconnect the client. A running Python daemon retains its already-imported
schema and worker defaults; editing files on disk does not reload those modules.
Also check explicit values in merged user/project/session configuration. No
migration or generated configuration is needed to enable the omitted-option
default.

The schema previously materialized omission as `false`, overriding the worker
dataclass's `true` default during `WorkerConfig.from_section`. Both defaults must
agree. Built-in defaults contain only the config version; typed section defaults
are applied after recursive layer merging. An explicit boolean `false` in a
lower layer therefore survives an upper layer that merely omits the option.

## Quick Gemini read-only workers

The currently supported agy CLI has no enforceable read-only sandbox or external
tool-restriction contract: `--mode plan` is not verified to prevent writes.

Nexus does not enforce read-only on any provider (user decision 2026-10-17):
"read-only" is system-prompt guidance only. A read-only role such as `advisor`, a
read-only tool list, or a `tools: []` Task call plans and runs on Antigravity like
any other child, with agy's own tools, and a tier whose first model is Antigravity
is never skipped for it. Earlier versions refused these with "antigravity
children cannot be read-only"; that refusal no longer exists. Settings
inheritance still does not grant permission to bypass restricted mode, tool
restrictions or an explicit sandbox request, which remain refused.
