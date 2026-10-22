# NVIDIA provider — current implementation and verification

**Status: implemented and verified offline; live API validation pending.**
The catalogue-selection and slash-ID routing issues are resolved offline. This
ledger describes the current worktree, not a live-provider certification. No
actual NVIDIA request was made and no live credentials were supplied or used
for this update.

## Current implementation

### Adapter and credentials

- Provider ID / adapter kind: `nvidia`, for NVIDIA's hosted catalogue
  (build.nvidia.com).
- [`nexus/model/providers/nvidia.py`](../../nexus/model/providers/nvidia.py)
  defines `NvidiaProvider`, a thin preset over the shared `OpenAIProvider`:
  `https://integrate.api.nvidia.com/v1`, Chat Completions only
  (`/chat/completions`, not the Responses API).
- The adapter defaults to `NVIDIA_API_KEY` in the daemon environment; explicit
  `api_key` and a pasted key stored through Settings (`auth = "keychain"`) are
  also supported. [`nexus/host_support/provider_auth.py`](../../nexus/host_support/provider_auth.py)
  provides the pasted-key flow, and [`nexus/runtime.py`](../../nexus/runtime.py)
  wires the provider into runtime construction.
- [`nexus/host_support/setup.py`](../../nexus/host_support/setup.py) recognizes
  `NVIDIA_API_KEY` as connected for setup, persists `${env:NVIDIA_API_KEY}` rather
  than the secret, selects `api = "chat"`, and clears stale keychain auth when
  selecting the environment-key route. Settings can instead save a pasted key
  with the NVIDIA base URL and keychain auth. A connected/setup status means a
  credential is present, not that NVIDIA has accepted it.
- The adapter adds `X-BILLING-INVOKE-ORIGIN: Nexus` unless overridden by a
  same-named user header. It has no token-count endpoint. Shared chat decoding
  handles `reasoning_content`; this is covered with synthetic streams, not
  observed live. `NVCF-POLL-SECONDS`, image, embedding and classifier APIs are
  outside the implemented scope.

### Vetted default and offline catalogue

Only `nvidia/llama-3.1-nemotron-ultra-253b-v1` is currently vetted for automatic
NVIDIA setup. The exact current sources are:

- [`nexus/host_support/setup.py`](../../nexus/host_support/setup.py): `_models`
  returns only that wire ID for NVIDIA, rather than automatically selecting
  arbitrary newer catalogue entries.
- [`nexus/model/data/models.min.json`](../../nexus/model/data/models.min.json):
  the bundled NVIDIA catalogue contains that single model record.
- [`tests/fixtures/nvidia-models-dev.json`](../../tests/fixtures/nvidia-models-dev.json):
  the preserved NVIDIA models.dev fixture contains the same single record;
  a regression test asserts equality with the bundled NVIDIA models.

The prior implementation notes identify the workspace models.dev caches
(`.nexus/cache/models.dev.json` and `benchmark/.nexus/cache/models.dev.json`)
as the inspected source and record that model metadata was copied verbatim.
The fixture preserves catalogue data (including costs, limits, dates and
capability flags), not a captured NVIDIA API response. These metadata values
are not independent proof of current pricing, endpoint availability or working
capabilities. The earlier `openai/gpt-oss-20b` NVIDIA snapshot/fixture entry was
removed because there was insufficient NVIDIA-specific endpoint evidence; it
is not an approved default. “Vetted” here describes the bounded offline
selection, not successful live execution.

Bundled data and the explicit setup default support first-run selection without
network access or an existing models.dev cache. No claim is made that the full
NVIDIA catalogue is chat-compatible or live-validated.

### Canonical references and routing

The canonical Nexus reference is
`nvidia/nvidia/llama-3.1-nemotron-ultra-253b-v1`: the first `nvidia/` selects the
Nexus provider route; the remaining `nvidia/llama-3.1-nemotron-ultra-253b-v1` is
the complete upstream wire ID. More generally, prepend `nvidia/` to the full
vendor/model wire ID. The repeated namespace is intentional, not a duplicate
wire prefix.

[`nexus/model/registry.py`](../../nexus/model/registry.py) maps NVIDIA to its
preset ahead of the generic OpenAI-compatible mapping and treats its catalogue
IDs as opaque during canonical grouping. Public qualification and lookup keep
the explicit NVIDIA route; generic lookup cannot silently reinterpret an
NVIDIA-hosted namespaced wire ID as another provider reference. Self-prefixed
direct IDs remain intact while cross-provider aggregator aliases are retained.
Setup writes the route prefix; [`nexus/model/router.py`](../../nexus/model/router.py)
removes only that prefix. The request body therefore receives the exact original
wire ID, never the complete Nexus reference or a vendor's direct-provider route.

## Verification

The latest **parent-reported focused run** is **443 passed, 291 skipped**,
superseding all earlier counts in this ledger. Its scope included NVIDIA,
provider conformance, setup/auth, registry/router, and docs/examples checks
(`tests/test_nvidia_provider.py`, `tests/test_provider_conformance.py`,
`tests/test_host_setup.py`, `tests/test_host_provider_auth.py`,
`tests/test_model_registry.py`, `tests/test_model_router.py`, `tests/test_docs.py`
and `tests/test_examples_valid.py`). Ruff also passed on the parent's specified
setup/auth/registry and test files. These are parent-run results, not a new test
or lint run performed by this document-only update.

Offline coverage includes mocked endpoint/header/auth and reasoning streams;
runtime construction; environment and pasted-key setup; exact raw catalogue ID
→ registry/reference → persisted config → router → mocked HTTP `model`;
no-cache/offline first-run defaults; and preservation of other provider aliases.
The fixture's provenance/equality is separate from synthetic request/stream
fixtures. Skipped tests are not evidence of verified behavior.

## Open risks and optional live validation

No offline implementation blocker remains in the scope described above.
**Live authentication, model availability, real tool calls, reasoning behavior,
usage/finish semantics and NVIDIA's handling of the attribution header remain
unverified.** Offline tests cannot establish credential acceptance, account
entitlements, service compatibility or current catalogue availability.

Only optional, explicitly authorized live validation remains on this checklist:

- [ ] Supply a real key securely and confirm authentication and access to the
  exact Nemotron wire ID; do not log or persist the key in test output.
- [ ] Make an ordinary text request and confirm the endpoint and exact wire
  model, streamed output, usage and finish behavior.
- [ ] Exercise reasoning where supported and record the actual stream shape.
- [ ] Exercise a tool call and tool-result continuation on the selected model.
- [ ] Check credential/error redaction and header behavior where observable;
  do not infer server-side billing-header use from a sent header alone.
- [ ] Record live observations separately from mocked evidence and retain
  “not verified” labels for any untested capability.

Implementation guidance and provider documentation:
[`docs/adding-a-provider.md`](../../docs/adding-a-provider.md),
[`docs/models.md`](../../docs/models.md) and
[`docs/provider-onboarding.md`](../../docs/provider-onboarding.md).
