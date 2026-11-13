# nexus.santa.build: install script and docs site (planning draft)

**Draft, not a numbered plan (user, 2026-11-12: "we need to discuss more before we make it concrete").** Nothing here is
decided beyond the table below; see [discussion-log.md](discussion-log.md) for how each point came up. Promote to a numbered file
in `plans/` when the open questions are settled.

**Request (user, 2026-11-12).** `curl -fsSL https://nexus.santa.build/install.sh | sh` installs Nexus, and the same site
carries the install docs, starting with how to put the iOS app on your own iPhone. Plan only; nothing is built yet.

## Decisions made (user, 2026-11-12)

| Question | Answer |
| --- | --- |
| How `/install.sh` is served | **302 redirect** to the GitHub release asset. GitHub stays the one source of the script; nothing to keep in sync. |
| Where the site's source lives | **A separate public repo** (not a folder in this one). |
| Where it is hosted | The `build-box` / `app` VM, like the other `*.santa.build` sites (see [../santa.build](../../../santa.build/AGENTS.md)). The user asked whether Cloudflare Pages would be better: see "Hosting" below. |
| iOS page | **Build-it-yourself only** (the signed development build). No TestFlight, no PWA page. |
| `uv` | **The installer installs `uv` itself** when it is missing (user, 2026-11-12: Core, the Monty kernel and the rest are Python, and `uv` also provides the Python, so a user needs nothing first). See "Installing `uv`". |
| Separate site | Yes, a separate subdomain and repo (user question, 2026-11-12). See "Security of the front door". |
| Day-one extras | **Version pinning** (`/v0.12.0/install.sh`), **uninstall and update docs**, and a **read-the-script-first** option next to the pipe-to-`sh` command. No Windows page. |

## What exists today (checked 2026-11-12)

- `install.sh` (POSIX `sh`) is attached to every GitHub release by [release.yml](../../.github/workflows/release.yml) with
  `SHA256SUMS`. The README command is
  `curl -LsSf https://github.com/nottherealsanta/nexus/releases/latest/download/install.sh | sh`. It downloads the Core wheel and
  the TUI binary from the same release, checks both against `SHA256SUMS`, and runs `uv tool install`. **The site never serves
  binaries and the script itself needs no change to be reached through it**: it reads `BASE`
  (`https://github.com/nottherealsanta/nexus/releases`), not the host it was fetched from.
- The release builds four TUI binaries ([build-artifacts.yml](../../.github/workflows/build-artifacts.yml)):
  `linux-x86_64` (ubuntu-22.04), `linux-aarch64` (ubuntu-22.04-arm), `darwin-arm64` (macos-15), `darwin-x86_64` (macos-15-intel).
  Each is smoke-tested with `--help` on its own runner. The installer's `uname` naming matches those four.
- Rejected on purpose: Alpine/musl (message in the installer) and Windows (`die`). The web bundle job is disabled
  (`if: false`), so a release has **no** `nexus web`; the docs must not promise it.
- The iOS flow is written in [nexus-nsh/README.md](../../nexus-nsh/README.md#install-on-an-iphone) and the
  [nexus-ios-device skill](../../.agents/skills/nexus-ios-device/SKILL.md). Its device recipe is still "unverified on device"
  ([05](../05-mobile-and-design.md)).

### Correction to "CI already covers every system"

Not quite, and the docs should not claim more than is true:

- CI's `lint` job runs `sh -n install.sh` and `scripts/test-install.sh` **once, on ubuntu-latest, offline** with a stub `uv`
  and `curl`. That tests the script's logic, not a real install.
- The only **real** install of a published release was macOS arm64, on 11-09 (0.12.0;
  [report](../../reports/release-0.12.0-validation.md)). **Linux x86_64, Linux aarch64 and macOS Intel have never run the published
  installer end to end.** Their binaries are built and smoke-tested with `--help`, nothing more.
- The `uv tool install` step builds `claude-agent-sdk` from source on the user's machine. That needs a working compiler path
  on each OS; only macOS arm64 has been seen to do it.
- The installer needs `uv` already installed and does not install it (`need uv`). On a fresh Linux box that is the first
  failure a user hits. Decided: the installer installs it (see "Installing `uv`").
- Repo rule: CI work is deferred ([README](../README.md)). The install smoke below is therefore a proposal that needs the user's
  go-ahead, not something to start.

## Design

### The site (new public repo)

Mirror `nottherealsanta/experiments`, which is the working example of a public repo deployed to `build-box`:

- A tiny Go server (stdlib only) embedding static pages, distroless image, `/healthz`, read-only container, caps dropped,
  `mem_limit: 128m`. Plain HTML and CSS; no JS needed for the content. Follow [DESIGN.md](../../../santa.build/DESIGN.md) in the
  `santa.build` repo for look and tokens.
- Suggested repo name `nexus-site` (open question below). Registry entry in `santa.build/subdomains.json`: subdomain `nexus`,
  `repo`, category `software` (or similar), `visibility: public`.
- **Routes**

  | Path | Response |
  | --- | --- |
  | `GET /install.sh` | `302` to `https://github.com/nottherealsanta/nexus/releases/latest/download/install.sh` |
  | `GET /v{X.Y.Z}/install.sh` (regex `^/v\d+\.\d+\.\d+(-[\w.]+)?/install\.sh$`) | `302` to `.../releases/download/v{X.Y.Z}/install.sh` |
  | `GET /` , `/install`, `/ios`, `/uninstall`, `/update` | the pages |
  | `GET /healthz` | `ok` |

  Anything else is a 404. The version is validated by regex and never concatenated from free input, so there is no open redirect.
- **Redirect response.** `Cache-Control: public, max-age=300` (a new release is live within minutes; the pinned form may be
  `max-age=86400`). The 302 **body** is a two-line shell snippet that prints "use `curl -L` (or `curl -fsSL`)" to stderr and exits 1, so
  someone who forgot `-L` and pipes the redirect body to `sh` gets an explanation and nothing is executed from it. The `-f`
  in the documented command makes a 404 fail loudly instead of piping an HTML page to `sh`.
- **No script bytes are served by the site.** That keeps the trust story simple: the script a user runs is exactly the file
  attached to the GitHub release, and `SHA256SUMS` in the same release covers the payloads.

### Security of the front door

A separate site is the better choice, and it is still **not safer than `github.com/.../install.sh` directly**. It adds one more party
that can change what a user runs. Be honest about both halves on the docs page.

Why separate (subdomain `nexus.santa.build`, own repo, own container) is better than a path on `santa.build`:

- **Own origin.** A compromised homepage, games or notes site cannot change what this host answers, and the install path
  shares no cookies or scripts with them.
- **Public repo, dispatch-only.** Like `experiments`, the repo only sends a `repository_dispatch`; the private `santa.build` runner
  builds it. A fork PR can never run code on `app`.
- **Tiny attack surface.** The server holds a fixed redirect table and static pages. No database, no user input reflected, no
  scripts served.

What the site adds to the trust chain (and the mitigations):

| Who could redirect an install | Mitigation |
| --- | --- |
| Whoever controls the Cloudflare account or DNS for `santa.build` | 2FA/passkey on the account; the install redirect is an **edge Redirect Rule with a hard-coded target**, reviewed like code |
| Whoever can push to the site repo or the deploy token | Branch protection and required review on `main`; the token is fine-grained, one repo; the redirect table is a constant covered by a test that fails on any host but `github.com/nottherealsanta/nexus` |
| A hostile `/vX.Y.Z/` path | Anchored regex, no free-form concatenation, tested with traversal and encoding cases (Steps B) |

What stays true regardless: the script is the GitHub release asset, every payload is checked against the release's `SHA256SUMS`, and the
site serves no bytes of its own. **A hosted script can only be trusted as far as its host**; the checksums protect the payloads from a bad
mirror, not the script from a bad redirect. For users who care, publish a build-provenance attestation for `install.sh`
(`actions/attest-build-provenance` in `release.yml`, verified with `gh attestation verify`) and mention it on the Install page. It
is optional, cheap, and the one check that does not trust the site. Not decided; listed as an open question.

### Installing `uv`

`install.sh` currently dies with "uv is not on PATH". Change: when `uv` is missing, install it, then continue. `uv` also downloads
and manages the Python Core needs, so the user installs no Python.

- **How.** Astral's official installer, with a **pinned version** so a Nexus release always installs a `uv` it was tested with:
  `curl -LsSf https://astral.sh/uv/<pinned>/install.sh | env UV_NO_MODIFY_PATH=1 sh`. The pin is a constant in `install.sh`, bumped on purpose
  (a release-checklist line, and Renovate if it is set up). Open question: instead download the pinned `uv` release tarball from
  `github.com/astral-sh/uv/releases` and check its published `.sha256`, which keeps the "every download is checksummed" rule; it is more
  script, so decide once the simple route is measured.
- **Do not touch the user's shell profile** (`UV_NO_MODIFY_PATH=1`). The installer calls `uv` by its full path
  (`$HOME/.local/bin/uv`, or wherever `UV_INSTALL_DIR` points) for the rest of the run and ends with the PATH hint it already prints, now
  also naming `uv`'s directory when it is not on PATH.
- **Say so and allow opting out.** Print "uv not found; installing it (https://astral.sh/uv)" before it runs. `--no-uv` (and
  `NEXUS_INSTALL_UV=0`) keep today's behaviour: stop with the message and the URL, for locked-down machines and distro-packaged `uv`.
  Piped from `curl` there is no stdin to prompt on, so this is flag-based, not interactive.
- **An existing `uv` is used as is.** Never upgrade or replace the user's `uv`. Optionally warn when it is older than the minimum
  that has `--no-binary-package` for `uv tool install` (check the exact version before writing the check).
- **Uninstall** does not remove `uv`: it may predate Nexus or be used elsewhere. `--uninstall` says so.
- **Still not covered:** compiling `claude-agent-sdk` from source needs a toolchain on some systems; this is its own step A check on bare
  containers. `uv` does not fix a missing compiler.
- **Tests.** `scripts/test-install.sh` (offline, stub `curl`/`uv`) gains cases: no `uv` installs it via the stub and continues; `--no-uv` stops
  with the URL; a failed `uv` download installs nothing else; a present `uv` is not touched. `sh -n install.sh` stays in CI.

### Pages

1. **Home / Install** (`/`): the prerequisites line (macOS or glibc Linux; x86_64 or arm64; `curl`; no `uv` or Python needed), the command, then:
   - *Read it first*: `curl -fsSL https://nexus.santa.build/install.sh -o install.sh`, `less install.sh`, `sh install.sh`. Also
     the GitHub release URL, printed as the direct fallback if the site is down.
   - Pin a version: `curl -fsSL https://nexus.santa.build/v0.12.0/install.sh | sh`.
   - After install: `nexus login …`, `nexus`, `nexus doctor`. Do not mention `nexus web` until the release has a web bundle.
   - Platform table (the four supported targets, Alpine and Windows listed as unsupported with the reason; WSL is untested, so say nothing
     about it).
   - Linux note: the TUI needs `libasound.so.2` (`libasound2` / `libasound2t64` / `alsa-lib`), with the exact message the installer
     prints.
2. **Update** (`/update`): re-run the installer (it restarts a running daemon), or pin a version. Today `nexus update` only checks and
   prints a URL; do not document it as an updater.
3. **Uninstall** (`/uninstall`): `sh install.sh --uninstall` needs the file, so the page shows
   `curl -fsSL https://nexus.santa.build/install.sh | sh -s -- --uninstall`. Say what it removes and that `~/.nexus` is never touched.
4. **iOS** (`/ios`): the build-it-yourself flow. See "iOS page" below.

### iOS page

Source of truth stays [nexus-nsh/README.md](../../nexus-nsh/README.md#install-on-an-iphone). The page is a shorter, ordered
walk-through of the same steps, and the **first thing it says** is what you need: a Mac with Xcode and the Nexus checkout,
an Apple developer account (a free account's profile expires in 7 days, so say so only after confirming it; see open questions), the iPhone's
UDID in the provisioning profile, and Core reachable from the phone (same Wi-Fi or Tailscale).

Rules for this page:

- **Placeholders only**: `<identity>`, `<team-id>`, `<profile>`, `<udid>`, `<host-ip>`. Never a real Apple ID, team, UDID, profile id,
  tailnet name or IP (the rule from the ios-device skill applies doubly to a public site).
- **Security warning up front**: `nexus daemon start` listens on `0.0.0.0` with no token for the phone, so use a trusted network or
  Tailscale only. This is the README's own caveat and must not be softened.
- **Honest status line**: it says the signed device build is "verified on device" only after that item in [05](../05-mobile-and-design.md)
  is ticked. Until then the page carries "not yet verified on a physical device".
- The mobile client is the cut-down one (Sessions, Manager, Settings). The page says that in one sentence so nobody looks for
  the file explorer. No iPad app (AGENTS.md, 2026-11-12): the iPad runs the web client.
- **Drift control**: the README, the skill and this page are three copies. Keep the page short and have it end with "Full steps and
  troubleshooting: nexus-nsh/README.md" linking the pinned tag. Add `last checked against Nexus vX.Y.Z` to the page footer and re-check
  on each release (a checklist line in the release steps, below).

### Hosting

The user asked whether Cloudflare Pages would be better. **Recommendation: stay on `build-box`, and put the one critical route at the
Cloudflare edge.**

- The `santa.build` repo's rule is "No Pages or Workers", and a site on `app` gets the existing deploy, rollback, health check, dashboard
  and tunnel with no new system. Pages would be the only thing in the estate on a different stack.
- The real risk for an installer is uptime. `build-box` is a home NUC. If the box, the tunnel or the power is down, `/install.sh` fails
  and a new user cannot install, even though GitHub is fine.
- Cloudflare is already the front door, so add **a Redirect Rule for `/install.sh` and `/v*/install.sh` at the edge**. It answers the
  302 without touching the origin, so the install command keeps working when the NUC is down, and no Pages or Workers are needed.
  The Go server keeps the same redirects so local development, tests and the no-Cloudflare path behave the same.
- The docs pages are cached by the CDN (`Cache-Control` set by the server), so a short outage mostly does not show.
- Use Pages instead only if the user wants the install page to stay up with the box entirely offline *and* is willing to
  break the "no Pages" rule; the cost is a second deploy path. This plan does not need it.

### Deploy wiring (same shape as `experiments`)

1. New **public** repo with its own `Dockerfile`, `compose.yaml` (`127.0.0.1:<port>:8080`) and `.github/workflows/deploy.yml` that only
   sends `repository_dispatch` (`nexus-site-push`) to `nottherealsanta/santa.build`. A public repo must **never** use the self-hosted
   runner.
2. `santa.build/.github/workflows/nexus-site.yml`, a copy of `experiments.yml`: `uses: ./.github/workflows/deploy.yml` with
   `name: nexus-site`, the new port and `repository`/`ref: main`.
3. Secret `SANTA_BUILD_DISPATCH_TOKEN` in the new repo (fine-grained PAT, `santa.build` only, Contents read/write). A free port:
   8080 homepage, 8082 experiments, 8083 notes, 8090/8091 games, 8092 notes-write are taken; **8084 is proposed**, to confirm against
   the registry.
4. `subdomains.json` entry (with the free port), then the tunnel ingress rule for `nexus.santa.build` and a proxied CNAME to the `app`
   tunnel, validated with `cloudflared tunnel ingress validate` as described in the `santa.build` AGENTS.md.
5. Cloudflare Redirect Rule for the two install paths (above). Cache rule: HTML 5 minutes at the edge.

Nothing in this repo's `release.yml` changes: it still attaches `install.sh`, and the redirect resolves `latest` at request time.

## Risks

- **`latest` can 404 right after a release.** release-please creates the release first; the TUI builds (up to 45 min) and the upload
  of `install.sh` come after ([release.yml](../../.github/workflows/release.yml)). In that window
  `releases/latest/download/install.sh` 404s for everyone, the site and the old README command alike. Fix options for the user to choose:
  create releases as drafts and publish them after the `attach` job, or have the site redirect to a tag only once its assets exist.
  Not decided; check whether `release-please-config.json` supports a draft release before choosing.
- A moved or renamed GitHub repo breaks the redirect target; it is one constant in the site's server. The site is the one place to
  change it, which is part of why the front door is worth having.
- Pipe-to-`sh` trust. The mitigation is the read-first option, the checksum of every payload and the fact that the site holds no script.

## Steps

### A. Verify the installer on every platform (before announcing the URL)

- [ ] **`install.sh` installs `uv`** as designed above, with the offline test cases. Land this before the real install checks below,
  so they exercise it from a machine with no `uv`.
- [ ] **Real install check on the four targets.** Run the published installer (`/releases/latest/download/install.sh`, then through
  the site once it exists) in an isolated `HOME`, `PREFIX`, `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` and `NEXUS10_HOME`, then
  `nexus --help`, `nexus doctor`, `nexus daemon start`/`stop` on a loopback port and `nexus-tui --help`. Targets: linux-x86_64,
  linux-aarch64, darwin-arm64, darwin-x86_64. Plus bare `ubuntu:24.04`, `debian:12` and `fedora` containers to catch the missing
  `libasound` and missing compiler paths (and the no-`uv` path, now that the installer installs it). Same procedure as the 0.12.0 validation; never the real home.
- [ ] **Proposed, needs the user's go-ahead because CI is deferred:** a `workflow_dispatch` + post-release `install-smoke.yml` that runs the
  above on the four GitHub-hosted runners and fails the release check if a platform cannot install. Until then, a manual run per release.
- [ ] Record results in `reports/install-site-2026-11-xx/`, with "not verified" where a platform was not run.

### B. Site (new repo)

- [ ] Create the repo from the `experiments` shape (Go server, Dockerfile, compose, dispatch workflow).
- [ ] Implement the routes and the redirect body. Go tests: each route, the version regex (good and hostile inputs such as
  `v1.2.3/../x`, `v1.2.3%2f..`, `V1.2.3`, a trailing slash), the redirect `Location`, the `-L`-less body, `/healthz`.
- [ ] Write the four pages (Home/Install, Update, Uninstall, iOS) in the site's design language; light and dark; mobile layout;
  no JS required for the commands, with a copy button as progressive enhancement.
- [ ] Wire the deploy (`santa.build` workflow, registry entry, tunnel rule, CNAME, Redirect Rule, secret). Deploy a hello page first
  to prove the pipeline, then the real content.

### C. This repo, after the site is live

- [ ] README `## Install`: lead with `curl -fsSL https://nexus.santa.build/install.sh | sh`, keep the GitHub URL as the fallback
  line and the checkout route as is.
- [ ] `install.sh` header comment and `docs/development.md` row: the new URL first. `scripts/test-install.sh` is unaffected (it sets
  `NEXUS_RELEASE_BASE`); keep `sh -n install.sh` in CI.
- [ ] `docs/porting-ledger.md` row only if installed behavior changes; it does not here.
- [ ] Release checklist line: re-check the site's iOS page and the platform table against the new release.
- [ ] Add a one-liner to [landed.md](../landed.md) and move this file to `done/` when A–C are ticked.

## Open questions

1. **Repo name**: `nexus-site`, `nexus.santa.build`, or other? (Default `nexus-site`.)
2. **Free vs paid Apple account** on the iOS page: has the dev build ever been done with a free account (7-day expiry, no
   Tailscale-style entitlements)? If unknown, the page says "an Apple developer account" and nothing about free tiers.
3. **`uv` install route**: Astral's pinned official installer (default), or the pinned tarball with its `.sha256` checked?
4. **The release window 404** (Risks): drafts-then-publish, or accept it for now?
5. **Smoke workflow** (step A): may it be added despite "CI is deferred", or manual runs only?
6. **Provenance for `install.sh`**: add a build-provenance attestation to `release.yml` and mention `gh attestation verify` on the page?
   (Touches the release workflow, which is CI work the repo has deferred.)
