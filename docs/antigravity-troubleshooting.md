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
argv. Preflight checks the version and required streaming CLI flags/values before starting
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
rules, pass `--dangerously-skip-permissions` or answer prompts; edit agy's own
settings with a narrowly scoped rule and re-run.

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
tool allowlist. `--mode plan` is an execution mode, **not** a filesystem security
boundary; inherited MCP servers/plugins can still have authority. Consequently
Nexus refuses a read-only Antigravity worker before launching agy. Quick Gemini
read-only requests must use a backend with enforceable read-only support instead,
or remain refused. Settings inheritance does not grant permission to bypass
read-only, restricted mode, tool restrictions or an explicit sandbox request.
