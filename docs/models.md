# Models

`nexus/llmhub/catalog.py` reads the [models.dev](https://models.dev) catalog and
lists, per provider, the models that can run an agent turn. `llmhub/hub.py`
and `cli.py` turn a `provider/model` reference into a provider and a model id.

## Catalog: download, cache, fallback

| Item | Value |
| --- | --- |
| URL | `https://models.dev/api.json` |
| Cache file | `~/.nexus10/cache/models.dev.json` (`$NEXUS10_HOME/cache/...`) |
| TTL | 24 h (`TTL_S`), measured from the file's mtime |
| Download limits | 30 s total, 10 s connect, 64 MiB (`MAX_BYTES`) |

`load(refresh=False)` returns `(catalog, source)`:

| Situation | Result | `source` text |
| --- | --- | --- |
| Cache younger than TTL, no `refresh` | Cache (a corrupt cache file counts as missing) | `cache (N min old)` |
| Cache old or missing, or `refresh=True` | Download, then write the cache (temp file + rename) | `models.dev (downloaded now)` |
| Download fails, a cache exists (any age) | Stale cache | `stale cache (N h old; download failed: ...)` |
| Download fails, no cache | `OSError("models.dev is unreachable and there is no cache: ...")` | n/a |

Download failures that trigger the fallback: `OSError` (HTTP status >= 400, too
large), `ValueError`, `TypeError` (not a JSON object), `httpx.HTTPError`.

`nexus models [provider] [--all] [--refresh]` prints the source line first, then
one block per provider. Without `--all`, providers that are not connected show
only a "not connected" line.

## Which models are listed

`models_for(catalog, provider_id)` reads `catalog[<models.dev id>]["models"]`
(ids in [llm-providers.md](llm-providers.md)) and keeps an entry only if all of
these hold:

1. It is an object with a truthy `tool_call`.
2. `status` is not `"deprecated"`.
3. `modalities.output` includes `"text"` (a missing list counts as text).
4. The model id contains none of `NOT_AGENT_MODELS`:
   `realtime`, `audio`, `transcribe`, `tts`, `search-preview`, `embedding`
   (substring match).
5. For `codex` only: the id starts with `gpt-` and `reasoning` is truthy.
   Codex has no models.dev entry of its own; it uses the `openai` list. Not every
   listed id is served by the Codex backend (not verified per id).

The result is sorted by `released` then `id`, newest first.

## `ModelInfo`

Frozen dataclass.

| Field | Source in the models.dev entry | Notes |
| --- | --- | --- |
| `provider` | the Nexus provider id | e.g. `grok` |
| `id` | the key in `models` | |
| `name` | `name` | falls back to `id` |
| `context` | `limit.context` | tokens, may be `None` |
| `max_output` | `limit.output` | tokens, may be `None` |
| `reasoning` | `reasoning` | bool |
| `efforts` | `reasoning_options` entry with `type == "effort"`, its `values` | empty tuple if none |
| `input_cost` | `cost.input` | USD per million tokens |
| `output_cost` | `cost.output` | USD per million tokens |
| `cache_read_cost` | `cost.cache_read` | USD per million tokens |
| `tiers` | each `cost.tiers[].tier.size` with `type == "context"` | ascending prompt sizes where the input price rises; empty if none |
| `released` | `release_date` | string |
| `ref` (property) | | `"{provider}/{id}"` |

In `nexus models`, the reasoning column shows the effort values, else `yes` or `no`.

## Model references

A model is named `provider/model`, for example `grok/grok-4.7`.

`hub.split_ref(ref)`:

* `grok/grok-4.7` returns `("grok", "grok-4.7")`. Only the first `/` splits, so a
  model id may itself contain `/`.
* No `/` means codex: `gpt-6.1-terra` returns `("codex", "gpt-6.1-terra")`.
* An unknown provider before the `/` raises `ValueError` listing the known ones.

`hub.provider_for(ref)` returns `(provider, model_id)` where the provider comes
from `providers.make(provider_id, SIGN_INS[provider_id]())`.
`hub.connected()` lists provider ids (in table order) whose sign-in reports
`logged_in()`.

## Default model

`cli._default_model()`, used when `--model` is not given:

1. `$NEXUS_MODEL`, used as given (it goes through `split_ref`, so a bare id means codex).
2. Otherwise, over the connected providers: `codex` if connected, else the first
   connected one, and the result is `"{provider}/{default_model}"`.
3. If nothing is connected, `codex` is assumed (the run then fails with a "not
   logged in" `AuthError`).

Defaults come from the provider table: codex `gpt-6.1-terra`, grok `grok-4.7`,
copilot `claude-sonnet-5`, opencode `kimi-k3`.

The chosen model id is not checked against the catalog; the provider rejects an
unknown one.
