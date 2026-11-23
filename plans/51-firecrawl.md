# 51. Firecrawl Local for scraping, documents, map and crawl

User request (2026-11-23): "let's have firecrawl-local be part of nexus for scraping, pdf/docs, map, crawl, not using
other features". Chosen with the user the same day: **one tool** `firecrawl {action: scrape|map|crawl}`, **public addresses
only**. The user's guess that it is "just one binary" was right: `apps/local` in the Firecrawl repo is `firecrawl-local`,
one Rust binary (crate `firecrawl-local` 0.1.1, AGPL-3.0). The Docker stack in the self-hosting guide (API, workers,
Playwright, Redis, RabbitMQ, Postgres) is not used. Behaviour is in [tools.md](../docs/tools.md#firecrawl).

## Decisions

- **Binary, not Docker.** No containers, queue or database. **Releases ship it** (user, 2026-11-23: users must not build it):
  `build-artifacts.yml` builds the pinned crate per platform, `release.yml` attaches the notice, `install.sh` installs it.
  `nexus firecrawl init` (cargo, about 4 minutes cold) is only the fallback. Core never installs it; a missing binary is an
  error that tells the model not to retry.
- **Cloud off.** `firecrawl-local` sends pages it cannot fetch (and search, Alexandria) to Firecrawl's cloud by default,
  without an account. Nexus sets `FIRECRAWL_CLOUD=off`, so the user's URLs never go to a third party.
- **Server, not the CLI.** `firecrawl-local serve` on a free loopback port with a random token, started by the first call
  and stopped with the daemon. Crawl needs the server's job API (the CLI's `crawl` only prints at the end, with no cap on
  time). Not used: `mcp` (needs Node and `npx`), `search`, `login`, `data`, `init`.
- **Public only.** The server's default policy (it refuses loopback, private, link-local and metadata addresses after DNS
  and at redirects, and filters the browser's subresources). Nexus never passes `--allow-private`.
- **No start button.** Nothing is running to start or stop in Settings, so unlike `web_search` there is no Settings row,
  endpoint or status line. The tool is in the Tools grid in both clients. Revisit if the user wants an install button.

## Done (2026-11-23)

- [x] `nexus/firecrawl.py`, `nexus/tools/firecrawl.py`, `nexus firecrawl init|status`, `nexus doctor` row, permission key,
  side questions and forks allowed, shutdown stops the process.
- [x] Both clients: Tools grid chip, `Firecrawl` timeline label and argument, web icon, `page` activity noun (TUI and NSH
  edited together; the manifest note regenerated into `nexus-nsh/src/gen/manifest.ts`).
- [x] 14 tests with a stand-in server (`tests/test_firecrawl.py`); Core suite and `cargo test` pass.
- [x] Live run of the crates.io build: example.com scrape, crawl and map; an arXiv PDF as Markdown; docs.firecrawl.dev map;
  `127.0.0.1`, `localhost` refused; a 404 reported as an error; no process left behind.

## Not verified

- [ ] A page that needs JavaScript (the Chrome path). The sandboxed Chrome on macOS and Linux, and `FIRECRAWL_CHROME_PATH`
  for a Chrome in a non-standard place, are untested.
- [ ] Linux and a machine with no Chrome.
- [ ] NSH: the test runner (`nsh`) is not installed here, so NSH's settings and timeline edits are checked by reading only.
  Paired TUI/NSH screenshots of the Tools grid and a `firecrawl` row are not captured.
- [ ] Whether a model picks `firecrawl` over `web_fetch` sensibly; look at real sessions after a week.
- [ ] **Release path unrun.** `scripts/test-install.sh` covers the installer offline (passes), but the new `firecrawl` job in
  `build-artifacts.yml` has never run on CI: the Linux (glibc 22.04, x86_64 and arm64) and macOS x86_64 builds of
  `firecrawl-local` are untried, and no real release has carried it. Run the workflow's `workflow_dispatch` dry run before the
  next release. Not tried: a downloaded macOS binary being quarantined or needing signing.
- [ ] A release with no firecrawl binary (older releases) leaves the tool saying it is not installed; `install.sh` prints the
  `nexus firecrawl init` hint.
- [ ] AGPL-3.0: Nexus now **redistributes** the unmodified binary, so the AGPL's source offer applies to it. The release
  carries `THIRD-PARTY/firecrawl-local.md` (licence, pinned version, where the source is); Nexus talks to it over HTTP as a
  separate program. Needs the user's or a lawyer's sign-off before the first release that includes it; this is not legal advice.
- [ ] Upgrades: the version is pinned once, in `nexus/firecrawl.py` (`VERSION`); the release job, `install.sh` and `init` all
  read it. Moving it is a manual, tested change.
