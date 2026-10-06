# Context header sections: a clean system prompt, skill/MCP inspectors, a thin Tools list, and mock extensions

Status: done for the native Ratatui client (2026-10-06). Implemented and checked in a real PTY
(`nexus --dev chat`) and by tests: header order, greyed empty blocks, agent-colour titles with
a thin rail, the thin Tools list and tool page (Space toggles in place), Skills and MCP cards,
the skill page, the MCP server page, host row fields, the dummy MCP server, seeds and the
`extensions` scenario. **Not verified:** the GPUI desktop client (the `lines`/`trailing`
rendering in `rust/desktop/src/panels.rs` is uncompiled here: the Metal shader toolchain is
missing; desktop has no Space-to-toggle on the tool page), the subagent-page read-only walk,
the 80/120/200-column width sweep, and screenshots under `artifacts/`. The five-row inventory
cap is silent (see docs/decisions.md). Original text follows. Native Ratatui client first; the GPUI desktop client
receives the same dialogs through the shared bridge (`nexus/ui/ratatui/desktop.py`)
and gets a visual check. The web client is not touched (to be deprecated).

## Goals

1. **System prompt shows only the system prompt.** The `System prompt` section
   stops showing the environment block, the skills index and the other parts that
   are folded into `system_text` today. What it does show is exactly the identity
   and `SOUL.md`/agent instructions.
2. **Skills section shows each skill's frontmatter** with two token figures: what
   the skill costs *in context now* (its index line) and what the *whole skill*
   costs if loaded. Clicking a skill opens the full `SKILL.md`, rendered as Markdown.
3. **MCP section works the same way.** Each server gets a card with its status,
   tool count and token figures. Clicking a server opens a server page with a
   one-line-per-tool list, and clicking a tool opens its full definition.
4. **Tools becomes a thin, one-line-per-tool list.** Each row shows the name, the
   tokens and an on/off toggle. Clicking a tool opens a second modal with
   everything about that definition.
5. **Mock mode ships extensions.** Mock mode adds several project and global
   skills and a dummy MCP server (plus a second "search"-loaded server and one
   that fails on purpose). These exist only in the dev sandbox. A mock scenario
   exercises them, and we verify the whole flow by running `nexus --dev chat`.

Nexus's principle still applies: **nothing the agent sees is hidden**. Removing
text from the System prompt view means moving it to a section where it belongs,
not dropping it (see §1.2).

## Current state

| Area | Where | What happens today |
| --- | --- | --- |
| System prompt text | `ui_support/context.py:21` `header_system_prompt` | Returns `system_text` minus `agents_md` only. That means identity, soul, **environment**, **skills_index**, **mcp_index** and **memory** are all shown as "System prompt". |
| System prompt tokens | `ui_support/context_header.py` `header_blocks` | `estimate_tokens(prompt)` over that same text. The skills index is therefore counted **twice** (System prompt and Skills). The MCP index is counted in System prompt and MCP tool tokens are counted again under MCP. |
| Prompt parts | `context/parts.py` `PART_ORDER` | identity, soul, environment, tools, agents_md, skills_index, mcp_index, memory, attachments, history, user. `inspect_context` already returns them by name in `included_parts` (`runtime.py:2839`). |
| Skills dialog | `ui/ratatui/workflows.py` `context_extensions("skills")` | One row per skill, `scope name`, with a toggle. Enter shows `labelled(row)` (name/description/scope/origin). There's no frontmatter beyond the description, no token figures, and no way to read the body. |
| Skill rows from host | `runtime.py` `skills_index` list | `name, description, included, enabled, scope, origin`. There's no frontmatter, `body_size` or index line. |
| Skill bodies | `skills/manager.py`, `skills/frontmatter.py` (`body_size`) | Bounded reads already exist. There's no host command to read one for display. |
| MCP dialog | same `context_extensions("mcp")` | `scope name` plus a toggle. Enter shows the raw row. Tool schemas are only reachable through the Tools dialog. |
| MCP rows from host | `runtime.py` `mcp_servers` | `name, status, scope, enabled, tool_loading, schema_tokens, tool_count, tools[names]`. There's no transport, instructions, resources, prompts or per-tool schema. |
| Tools dialog | `workflows.py:859` `tools_modal` | `layout="context"`, which is the full transcript width minus 4 (`render/dialogs.rs:76`). Rows are grouped by family, with `▸/▾` headers and `name · detail[:60] · ~N`. Enter opens `tool_definition`, a plain menu of `entry.body` lines. |
| Disabled tools | `runtime.py` tools list, `enabled: False` rows | Sent with `input_schema: {}`. Their token estimate is wrong and their definition page is empty. |
| Panel sizes | `render/dialogs.rs` `panel_area` | Two sizes only: `context` (near full) and page/drawer. There's no narrow list layout. |
| Dev sandbox | `devtools/mock/sandbox.py` | One project skill (`mock-skill`) and no MCP. `ensure_sandbox` seeds **only on first creation**, so new seed files never reach an existing sandbox. |
| Offline MCP fixture | `benchmark/mcp_echo.py` | A minimal stdio echo server. It's a useful reference for the protocol subset we need, but it's benchmark-only. |

## Plan

### 1. System prompt shows only the system prompt

#### 1.1 Selection by part name, not by subtraction

- Replace `header_system_prompt` with `system_prompt_parts(result) -> list[(name, text)]`.
  It returns the `included_parts` whose name is in `SYSTEM_PROMPT_PARTS = ("identity", "soul")`,
  in `PART_ORDER` order. `header_system_prompt` keeps its signature and joins
  those parts, so callers (`workflows.py` `context_show`, `context_header.py`) change minimally.
- Fallback: when `included_parts` is empty or clipped (the join of all parts no
  longer equals `system_text`), keep today's behaviour and show the whole text. The
  dialog then opens with a labelled note: `Parts unavailable; showing the full
  system text, which includes environment, skills and MCP index.` This follows
  "never lose context that can't be reconstructed".
- The `System prompt` chip's token figure becomes `estimate_tokens` of the identity
  and soul parts only.
- The dialog title stays `System prompt · literal`. The body is rendered as
  Markdown (as AGENTS.md already is). When an agent definition supplies the
  instructions, a muted first line says so: `From agent: build (soul replaced)`,
  sourced from `preview.agent`.

#### 1.2 Where the removed parts go (nothing is hidden)

| Part | New home |
| --- | --- |
| `environment` | A new **Environment** row in the context header, placed after System prompt. It has a one-line preview and its own token figure, and clicking it opens the literal text in the same context dialog. |
| `skills_index` | The Skills section (§2). Each skill's card shows its own index line, and the section title carries the part's total tokens. A `Show literal index` row at the bottom opens the exact text sent. |
| `mcp_index` | The MCP section (§3). It has a `Show literal index` row, fenced and labelled untrusted as it is in the prompt. |
| `memory` | A **MEMORY.md** row, shown only when the part is included and non-empty. It has the same shape as AGENTS.md (Markdown dialog, `system_files["memory"]` source). |

Header rows stay in `PART_ORDER`: System prompt, Environment, Tools, AGENTS.md,
Skills, MCP, MEMORY.md (when present). Each row counts only its own part, so the
rows now sum to the `Context total` footer without double counting. Add a test for
that invariant (§Tests).

Open question A: should Environment be its own header row (the default in this
plan) or live only behind a `Show environment` link at the bottom of the System
prompt dialog? A separate row keeps the "nothing hidden" rule visible at a glance
and costs one header line.

### 2. Skills: frontmatter cards, then the full skill

#### 2.1 Host data (rule 4: through the host contract)

Extend each `skills_index` row in `inspect_context` (`runtime.py`). Everything
stays bounded and sanitized:

| Field | Meaning |
| --- | --- |
| `frontmatter` | The parsed declared fields as a flat `dict[str, str]`, sanitized and capped (≤ 32 keys, ≤ 300 chars per value). It covers `name`, `description`, `allowed-tools`, `version`, `model`, etc., in file order. |
| `index_line` | The exact `name: description` line in this request's `skills_index` part, or `""` when the skill is off or the line was budget-dropped. |
| `context_tokens` | `estimate_tokens(index_line)`, the cost **now**. |
| `skill_tokens` | Estimated cost of the full `SKILL.md` (frontmatter and body) if loaded, from the manifest's recorded size (`body_size` plus frontmatter bytes, divided by 4). No file read happens during preview. |
| `config_enabled` | Whether Settings disabled the skill. It mirrors the MCP rows. |
| `resources` | The count of bundled resource files (names come from `SkillShow`). |

New host command, `nexus/host/protocol.py` and `facade.py`:

```python
class SkillShow(msgspec.Struct, tag=True, frozen=True):
    session: str
    name: str

class SkillShowResult(msgspec.Struct, tag=True, frozen=True):
    name: str
    scope: str            # project | global
    origin: str           # relpath label, never an absolute home path
    enabled: bool
    frontmatter: dict[str, str]
    body: str             # Markdown body, bounded by the skill size limit
    truncated: bool
    bytes: int
    context_tokens: int
    skill_tokens: int
    resources: list[str]  # bundled file names, ≤ 64
```

- The facade resolves `name` against the session's **frozen manifest** snapshot,
  so the page shows the version the agent would load, not a newer file on disk.
  It reads through `skills/manager.py`'s bounded loader. It doesn't read files
  itself (security: tool paths go through permissions, and skill reads go through
  the skill manager's existing size checks).
- Errors (`unknown skill`, `skill changed on disk`, `too large`) are redacted and
  returned as normal host errors.
- Agent pages: `SkillShow` is available read-only. On a subagent page it uses the
  child's `agent_context` row data for the card and the same command for the body.

#### 2.2 Ratatui dialogs

**Skills section** (`context_extensions("skills")` becomes `skills_modal()` in a new
`ui/ratatui/context_sections.py`, per rule 3: new behaviour in its own module):

```
 Skills · 5 of 6 on · ~212 tokens in context
 ─────────────────────────────────────────────────────────────
 ● code-review                                         project
   description   Review a diff for correctness and style
   allowed-tools read, grep, glob        version  2
   ~38 in context · ~1.9K full skill · 2 resources
                                                       (blank)
 ○ release-notes                                       project
   description   Draft release notes from merged PRs
   ~0 in context (off) · ~860 full skill
 …
 Show literal index (~212 tokens)
 Edit skills…
```

- One card per skill, project first and then global, sorted by name. The heading
  line has a toggle (`●` on, `○` off, `🔒`-style muted mark when locked) plus the
  name and scope. Below it come the frontmatter fields as aligned `key  value`
  rows (`name` is omitted because it's in the heading; long values wrap and are
  never silently cut, with clipping announced as `…`). Last comes the token line.
- Enter or a click on the card opens the **skill page**. Space or a click on the
  toggle switches the skill (existing `context_toggle`, same lock rule).
- The card needs multi-line items. If `bridge.rs` `Item` can't already render a
  `description` block under the label, add `lines: Vec<String>` (dim, wrapped) to
  `Item` and make the hit-test cover the whole card height. Check this first in
  `render/dialogs.rs`; do not render cards as separate selectable items.

**Skill page** (`skill_show` operation): `layout="context"`, `format="markdown"`.

```
 Skill · code-review · project · .agents/skills/code-review/SKILL.md
 ● on · ~38 tokens in context · ~1.9K tokens full skill · 6.8 KB

 | Field         | Value                                  |
 | name          | code-review                            |
 | description   | Review a diff for correctness and style|
 | allowed-tools | read, grep, glob                       |

 # Code review
 …full body rendered by the existing Markdown renderer…

 Resources: checklist.md, examples/bad.diff
```

- The frontmatter is rendered as a Markdown table so it gets the same renderer.
  A truncated body ends with `Content truncated by the host (N of M bytes shown).`
- Esc returns to the Skills section with the same card selected
  (`self.back()` keeps the stack).

### 3. MCP: server cards, a server page, then a tool page

#### 3.1 Host data

Extend each `mcp_servers` row:

| Field | Meaning |
| --- | --- |
| `transport` | `stdio` or `http`. |
| `command_label` | The executable basename and argument count only, e.g. `python (3 args)`. **Never** env values, headers or URLs with credentials. |
| `index_line` | This server's lines in the `mcp_index` part. |
| `context_tokens` | Index line tokens plus, when `tool_loading == "all"` and the server is enabled, the schema tokens of its tools actually sent. |
| `schema_tokens` | Already present: all schemas, the cost if fully loaded. |
| `instructions_tokens` | The size of any server instructions, if the client keeps them. |
| `error` | A redacted connect error for `failed` servers. |

New host command:

```python
class McpServerShow(msgspec.Struct, tag=True, frozen=True):
    session: str
    name: str

class McpServerShowResult(msgspec.Struct, tag=True, frozen=True):
    name: str
    status: str
    scope: str
    enabled: bool
    transport: str
    command_label: str
    tool_loading: str
    tool_loading_source: str
    server_info: dict[str, str]        # name/version from initialize
    instructions: str                  # untrusted; shown fenced and labelled
    tools: list[dict[str, Any]]        # name, description, input_schema, annotations, sent: bool, tokens
    resources: list[dict[str, str]]    # uri, name, mime (≤ 256)
    prompts: list[dict[str, str]]      # name, description (≤ 256)
    error: str
```

The facade reads from `self._mcp.server_snapshot(name)` and never connects a
server just to show it. A lazily unconnected server shows `not connected yet` and
lists no tools; this is stated in the page, not hidden. Bounds: 2,000 tools (the
existing search index cap), with 64 KB per schema and clipping announced.

#### 3.2 Ratatui dialogs

**MCP section** has the same card shape as Skills:

```
 MCP · 2 of 3 on · ~540 tokens in context · ~3.1K deferred
 ● mock-tracker      connected · stdio · all         project
   python (3 args) · 6 tools · 2 resources · 1 prompt
   ~410 in context · ~410 full schemas
 ● mock-docs         connected · stdio · search      global
   python (3 args) · 24 tools
   ~40 in context · ~2.7K deferred (loaded via search)
 ○ mock-broken       failed                          project
   error: process exited (code 3) before initialize
 Show literal index (~130 tokens)
 Edit MCP servers…
```

**Server page** (`mcp_server_show`, thin list layout, §4.2):

- Header lines: status, transport, scope, tool loading and its source, server
  name/version, and token figures.
- Then `Instructions (untrusted, from the server)` as a foldable block when present.
- Then one line per tool, in the §4 format (`name · ~tokens · sent/deferred`).
  Enter opens the shared **tool page** (§4.3), with MCP extras: annotations, and
  whether the tool is sent or found via search.
- Then resources and prompts as one-line rows. Enter shows their labelled details.

Per-tool toggles for MCP tools work exactly like built-in tools, since they're
in the `tools` category already (`ContextExtensionSelect` validates against
`context["tools"]`).

### 4. Tools: a thin one-line list, then a full definition page

#### 4.1 Host data fixes

- Disabled tools must carry their **full** definition. In `runtime.py` the
  `enabled: False` rows take `description` and `input_schema` from the tool
  manager's spec (`tool_specs[name].to_schema()`) or the MCP snapshot, not `{}`.
  Their token figure then shows what switching them back on would cost.
- Every tool row gains `source`. It's `built-in`, `extension` (with the relpath
  label of `.agents/tools/x.py`), or `mcp:<server>`. Rows also gain `bundle`
  (already present) and, where the spec has them, `read_only`, `permission`
  (the `tools/permissions.py` category: read, write, shell, network) and
  `timeout_s`. Each value comes from the spec. A missing value is omitted, not
  guessed.

#### 4.2 Thin list layout (Rust)

- Add a panel layout `"list"` in `render/dialogs.rs` `panel_area`. The width is
  `clamp(widest row + 6, 44, 72)`, capped at `transcript.width - 4`. The height is
  `min(rows + chrome, transcript.height - 2)`. It's centred in the transcript.
  Below 44 columns it falls back to `context` width.
- Row format (one line per tool, no family headers, no description):

```
 ╭ Tools · 18 of 20 on · ~6.4K tokens ─────────╮
 │ bash                              ~1.4K  ●   │
 │ edit                               ~620  ●   │
 │ glob                               ~210  ●   │
 │ mcp__mock_tracker__create_issue    ~340  ○   │
 │ webfetch                           ~280  ○   │
 │ Enter details · Space toggle · Esc close     │
 ╰──────────────────────────────────────────────╯
```

  The name is left-aligned and truncated with `…` (the full name is always on
  the detail page). Tokens are right-aligned in a fixed 6-column field, and the
  toggle sits in the last column. An off row is muted. When locked, the toggle
  shows a muted `·` and the footer says `Context locked after first turn`.
- Order: the order sent to the model (`preview.tools` order), with disabled tools
  in their family position rather than at the end. MCP tools keep their full
  `mcp__server__tool` name because that's what the model sees.
- Use the existing `Item.value` (or add `Item.trailing`) for the token column;
  don't encode it into `label` with padding. Rust owns the alignment so resizing
  works.
- Header chip unchanged: `Tools` still opens this list (`context_show` key `tools`).

#### 4.3 Tool page (second modal)

`tool_show` operation, `layout="context"`, `format="markdown"`. It's stacked on the
list, and Esc returns to the same row:

```
 Tool · edit · built-in · ● on · ~620 tokens
 Group  files        Bundle  core         Permission  write
 Read-only  no       Timeout  —

 ## Description
 …full description, Markdown…

 ## Parameters
 | Name | Type | Required | Default | Description |
 | path | string | yes | — | File to edit, relative to the workspace |
 | old  | string | yes | — | …                                       |
 | mode | replace \| insert | no | replace | …                         |

 ## Schema (as sent)
 ```json
 { …exact input_schema… }
 ```
```

- Space toggles from the page as well (same `context_toggle`). The title updates
  in place without stacking.
- Nested objects and arrays in the parameters table are flattened as
  `items.field`, so nothing hides behind `object`. Enum values are listed in full
  up to 32, with `… +N more` beyond that.
- `tool_entry` in `ui_support/context.py` gains a `page_markdown(tool)` helper
  that the MCP tool page reuses. `schema_param_rows` stays for the header.

Remove the old family-grouped `tools_modal`, `tools_toggle` and `tool_definitions`
paths, and `self.tools_expanded`, in the same change. Keep `tool_definition` as an
alias of `tool_show` only if a saved operation can still reach it; check with grep.

### 5. Mock mode: dummy MCP servers and several skills

Everything below lives under `nexus/devtools/` and the dev sandbox. The normal
product path never imports it (`docs/devtools.md` contract), so it's enabled only
in dev mode by construction.

#### 5.1 Dummy MCP server: `nexus/devtools/mock/mcp_server.py`

- A stdio JSON-RPC server, extended from the `benchmark/mcp_echo.py` protocol
  subset. It supports `initialize` (with `instructions`), `tools/list`,
  `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`
  and `ping`. It doesn't touch the network and only reads its own fixtures.
- `--profile` selects one of three identities:

| Profile | Purpose | Contents |
| --- | --- | --- |
| `tracker` | Rich schemas (`tool_loading: all`) | 6 tools: `list_issues` (enum `state`, `limit` default 20, array `labels`), `get_issue`, `create_issue` (nested `assignee` object), `add_comment`, `close_issue`, `search` (with annotations `readOnlyHint`). It has 2 resources, 1 prompt, server instructions and in-memory state seeded per process. |
| `docs` | Deferred loading (`tool_loading: search`), scale | 24 small `lookup_*` tools, so the deferred figure and `mcp_search` path show up. |
| `broken` | Failed status | Writes to stderr and exits with code 3 before `initialize`, so the card shows a redacted error. |

- Bounded: line length ≤ 1 MB, unknown methods get `-32601`, and every reply is
  one line of JSON.

#### 5.2 Seeding

- `sandbox.py` gains `generated_seed_files(home) -> dict[str, str]`, which returns
  the files that depend on the machine:
  - `<sandbox>/.agents/mcp.json`: `mock-tracker` (all), `mock-broken` (project
    scope). The command is `sys.executable` with
    `["-m", "nexus.devtools.mock.mcp_server", "--profile", …]`. It's absolute so
    the same interpreter with Nexus installed runs it.
  - `<dev home>/mcp.json`: `mock-docs` (global scope, search loading).
- Project skills under `<sandbox>/.agents/skills/`. Keep `mock-skill`, because
  the `stress` scenario calls it. Add:
  - `code-review`: long body with headings, a table and a code block, plus two
    resource files (`checklist.md`, `examples/bad.diff`). It tests Markdown
    rendering and resources.
  - `release-notes`: extra frontmatter fields (`version`, `model`,
    `allowed-tools`). It's disabled in the session by the scenario, to show the
    off state.
  - `sql-style`: a very long description, to test wrapping and the index line
    being budget-trimmed.
- Global skills under `<dev home>/skills/`: `writing-style` and `git-hygiene`,
  so both scopes appear.
- **Seed on every start, never overwrite.** `ensure_sandbox` writes any *missing*
  seed or generated file on each call, not only on first creation. Generated
  `mcp.json` files are rewritten when their content differs, the same way as
  `nexus.toml` today, because `sys.executable` moves between venvs.
  `restore_sandbox` also restores generated files. The seed commit includes the
  static skills. The generated `mcp.json` files go in the sandbox `.gitignore`,
  so a moved interpreter doesn't dirty the tree.
- `reset_sandbox` keeps its "refuse outside dev home" guard. Global seeds are
  written only inside the dev home (`dev_home()`), never `~/.nexus`.

#### 5.3 Scenario: `extensions`

`devtools/mock/scenarios/extensions.py` (listed in `_MODULES`) does the following:

1. Calls `skill` with `code-review`. The transcript shows the skill load.
2. Calls `mcp__mock_tracker__list_issues` with `state="open"`. This is a real
   stdio round trip.
3. Calls `mcp_search` for `lookup` and then one deferred `mock-docs` tool.
4. Ends with `✓ mock verdict — …` and checks that each tool result is non-error.

`tests/test_mock_scenarios.py` runs it like the others. It needs a real
subprocess, as the existing `benchmark_skill` MCP integration test does.

### 6. Verification in a mock directory (manual, then recorded)

1. Run `nexus --dev chat`, which uses the seeded sandbox at
   `~/.nexus/dev/sandbox/workspace`. Run `/mock clean` first if an old sandbox
   exists, then confirm the new seed files appear without a reset.
2. Before the first turn, open each header row and check the following:
   - **System prompt** has identity and soul only. There's no `<environment>`,
     no skill lines and no MCP index. Its token figure dropped accordingly.
   - **Environment** has its own row and dialog.
   - **Tools** is a thin list with one line per tool. Toggle `webfetch` off and
     back on. Open `edit` and check every parameter, the schema and the
     permission. Open a disabled tool and check that its schema is present.
   - **Skills** has 6 project and 2 global cards with frontmatter and both token
     figures. Open `code-review`, check that Markdown renders (table, code block),
     the frontmatter table appears and resources are listed. Toggle
     `release-notes`.
   - **MCP** has 3 cards: tracker connected/all, docs connected/search with a
     deferred figure, broken failed with an error. Open tracker, open
     `create_issue`, and check that the nested `assignee.*` parameters are listed.
   - Check that the header rows sum to `Context total`.
3. Run `/mock extensions`, then confirm the lock note and that toggles are read-only.
4. Open a subagent page (`/mock parallel_subagents`) and check that its sections
   are read-only and show the child's request.
5. Widths: check the layout at 80, 120 and 200 columns. At 80 the thin list still
   fits, and below 44 it falls back to full width.
6. Desktop: run `nexus desktop --dev` (or the documented launcher) and take
   screenshots of the same five dialogs with the native screenshot skill
   (`skills/native-app-review`).
7. Save PTY captures and screenshots to `artifacts/context-sections/`.

## Tests

| Test | Covers |
| --- | --- |
| `tests/test_ui_support_context_header.py` (or the existing peer) | `system_prompt_parts` picks identity and soul only. There's a fallback note when parts are missing or clipped. Header rows sum to the context total (no double counting of skills/MCP index). Environment and MEMORY rows appear only when included. |
| `tests/test_host_facade.py` | `inspect_context` skill rows carry `frontmatter`, `index_line`, `context_tokens`, `skill_tokens`. `SkillShow` returns the body from the frozen manifest and refuses unknown names and oversize bodies. `McpServerShow` returns tools with schemas, redacts the command and env, and doesn't connect lazy servers. Disabled tools keep their full schema. |
| `tests/test_mcp_integration.py` | Dev mock server: the `tracker`, `docs` and `broken` profiles over real stdio. |
| `tests/test_mock_sandbox*.py` | New seeds appear in an existing sandbox without a reset. Generated `mcp.json` follows `sys.executable`. Global seeds stay inside the dev home. Restore brings generated files back. |
| `tests/test_mock_scenarios.py` | The `extensions` scenario passes end to end. |
| `tests/test_ratatui_workflows.py` | Tools list rows (one per tool, token value, toggle). `tool_show` page sections. Skills cards. `skill_show` Markdown. MCP section, server page and tool page. Lock state. Esc returns to the same row. |
| `rust/tui` unit tests | `panel_area` for `"list"` (clamp, fallback below 44 columns). Multi-line card hit-testing. Right-aligned token column under resize. |
| `tests/test_ratatui_pty_*.py` (new `..._context_sections.py`) | Real PTY: open each section, toggle a tool, open a tool and a skill page, Esc back. |
| `tests/test_layering.py`, `test_ui_layering.py` | `devtools` is not imported from product modules, and the UI reaches skills and MCP only through host commands. |

## Docs to update in the same change

- `docs/context.md`: what the System prompt section means (identity and soul) and
  the per-row token accounting.
- `docs/ratatui-parity.md`: a new section replacing the Tools/Skills/MCP dialog
  descriptions, plus the `list` layout.
- `docs/surfaces.md`: header rows (Environment, MEMORY.md) and section behaviour.
- `docs/host.md`: `SkillShow` and `McpServerShow`, with their bounds and redaction.
- `docs/extensions.md`: new skill and MCP row fields.
- `docs/devtools.md`: mock extensions, the `extensions` scenario, the seeding rule
  change and the dummy MCP profiles.
- `docs/module-map.md`: `devtools/mock/mcp_server.py`, `devtools/mock/scenarios/extensions.py`
  and `ui/ratatui/context_sections.py`.
- `docs/decisions.md`: (a) the System prompt section is identity and soul, and
  the other parts get their own rows; (b) the dev MCP server lives in devtools
  and is wired only by the dev sandbox's generated `mcp.json`.
- `docs/security.md`: a note that `McpServerShow` exposes only the command
  basename and argument count.

## Phases

### Progress — paused after backend and header work

Implemented in the worktree (not a completion claim for the whole plan):

- Full disabled-tool definitions remain inspectable, including capability
  metadata and registered MCP schemas; disabled tools stay out of dispatch.
- Read-only `SkillInspect`/`SkillInspectResult` (the `SkillShow` contract below)
  uses the pinned manifest snapshot, including disabled skills, redacted
  source/frontmatter/body, explicit bounds, and missing-body errors. Inspection
  does not activate a skill.
- Read-only `McpServerShow`/`McpServerShowResult` and a client helper expose
  existing server state without connecting or making discovery/tool calls.
  Snapshots include stored server info/instructions and discovered catalogue
  data where available. Transport details omit HTTP endpoints, headers and
  environment; stdio exposes only command basename and argument count.
  Oversized snapshots omit structured detail with an explicit clipping error,
  rather than returning a truncated raw JSON dump.
- Shared header helpers select core System prompt, Environment, AGENTS.md and
  MEMORY.md from named prompt parts only when those parts reconstruct the full
  system text exactly. Otherwise System prompt retains the full-text fallback
  and separate contribution estimates are unknown, avoiding double counting.
- Environment and MEMORY.md header rows and native context picker/viewer routes
  are wired. Skills estimates use the included index rather than the available
  catalogue. The richer tool/skill/server detail dialogs are not implemented.
- Regression tests were added for MCP snapshots, header splitting/fallbacks and
  native workflow routes. Host, extension, context and native-terminal docs were
  updated alongside the code. No web-client changes.

Verification status:

- The earlier backend slice had 236 targeted tests passing (one unrelated
  context-preview test deselected), Ruff and `git diff --check` passing.
- Final verification of the latest MCP/header changes is **not complete**.
  The last attempted combined test run executed no tests because it referenced
  nonexistent `tests/test_ui_context_view.py`; work then stopped at the user's
  request. Re-run with actual test paths before treating these changes as
  verified. No native PTY, desktop walkthrough or screenshot verification yet.
- Known earlier unrelated failures: the context-preview test matches the word
  "history" in the standing prompt, and documentation link checks reference
  moved desktop/TUI plan files. Do not conflate those with this plan's progress.

Remaining by phase:

- **Phase 1:** backend foundations substantially implemented; audit remaining
  skill/MCP/tool row metadata against §3–4 and verify the latest changes.
- **Phase 2:** mock MCP server, extension seeding, skills/scenario and baseline
  screenshots remain pending.
- **Phases 3–4:** Tools list/tool page and richer skill/MCP cards/pages remain
  pending; host inspection commands exist but are not yet those UI pages.
- **Phase 5:** named header split and viewer routes implemented; token-sum
  invariant and real native/desktop behavior still need verification.
- **Phase 6:** final tests, terminal/desktop walkthroughs, artifacts and remaining
  documentation/security/decision updates pending. Keep this plan in draft.


1. **Host data.** Prompt part selection, skill/MCP/tool row fields, `SkillShow`,
   `McpServerShow`, and the disabled-tool schema fix. Python tests.
2. **Mock extensions.** The MCP server module, seeding changes, skills and the
   `extensions` scenario. Mock tests. After this, `nexus --dev chat` shows real
   data in the *old* dialogs, which is a useful before/after baseline: take
   screenshots now.
3. **Tools list and tool page.** The Rust `list` layout, the token column and
   `tool_show`. PTY test.
4. **Skills and MCP sections.** Multi-line cards, the skill page, the server page,
   and reuse of the tool page.
5. **Header rows.** System prompt narrowing, the Environment and MEMORY.md rows,
   and the token-sum invariant.
6. **Verification and docs.** The §6 walkthrough in the terminal and on desktop,
   artifacts, and docs. Move this plan to `plans/done/`.

## Open questions (defaults chosen; change before phase 1 if wrong)

- **A. Environment placement:** its own header row (default) or only a link inside
  the System prompt dialog.
- **B. Tools list scope:** it lists every tool sent, including MCP tools (default,
  because it matches what the model sees), or built-in tools only, leaving MCP
  tools in the MCP section.
- **C. Skill token figure:** "full skill" is estimated from recorded sizes
  (default, so preview needs no file reads), or measured by reading each file at
  preview time.
- **D. Per-tool toggles on the MCP server page:** shown (default) or read-only
  there, with toggling only in Tools.
