# 51. Firecrawl Local for scraping, documents, map and crawl

User request (2026-11-23): "let's have firecrawl-local be part of nexus for scraping, pdf/docs, map, crawl, not using
other features". Chosen with the user the same day: **one tool** `firecrawl {action: scrape|map|crawl}`, **public addresses
only**. The user's guess that it is "just one binary" was right: `apps/local` in the Firecrawl repo is `firecrawl-local`,
one Rust binary (crate `firecrawl-local` 0.1.1, AGPL-3.0). The Docker stack in the self-hosting guide (API, workers,
Playwright, Redis, RabbitMQ, Postgres) is not used. Behaviour is in [tools.md](../docs/tools.md#firecrawl).

## Changed release source (user, 2026-11-24)

Firecrawl stays a **native binary**, pinned at **0.1.1**, but Nexus no longer builds or serves it. `install.sh` (release
and checkout) downloads `firecrawl-local-<os>-<arch>` and `SHA256SUMS` from the public
[nottherealsanta/firecrawl-local-builds](https://github.com/nottherealsanta/firecrawl-local-builds) release `v0.1.1`,
verifies the SHA-256 checksum and then installs it in `$PREFIX/bin`. A missing release, a platform with no asset or a
failed download warns and installs Nexus without the tool; a checksum mismatch stops the install before anything is
installed. `build-artifacts.yml` no longer builds `firecrawl-local`, and `release.yml` no longer attaches it or its notice
to a Nexus release. `nexus firecrawl init` remains the cargo fallback, and `THIRD-PARTY/firecrawl-local.md` records the
licence, version and external source.

## Decisions

- **Binary, not Docker.** No containers, queue or database. **Nexus install downloads it** (user, 2026-11-24: users must
  not build it): `install.sh` fetches the pinned 0.1.1 binary from its own public release and checks that release's
  `SHA256SUMS`. Nexus CI does not build or serve it. `nexus firecrawl init` (cargo, about 4 minutes cold) is only the
  fallback. Core never installs it; a missing binary is an error that tells the model not to retry.
- **Cloud off.** `firecrawl-local` sends pages it cannot fetch (and search, Alexandria) to Firecrawl's cloud by default,
  without an account. Nexus sets `FIRECRAWL_CLOUD=off`, so the user's URLs never go to a third party.
- **Server, not the CLI.** `firecrawl-local serve` on a free loopback port with a random token, started by the first call
  and stopped with the daemon. Crawl needs the server's job API (the CLI's `crawl` only prints at the end, with no cap on
  time). Not used: `mcp` (needs Node and `npx`), `search`, `login`, `data`, `init`.
- **Public only.** The server's default policy (it refuses loopback, private, link-local and metadata addresses after DNS
  and at redirects, and filters the browser's subresources). Nexus never passes `--allow-private`.
- **No start button.** Nothing is running to start or stop in Settings, so unlike `web_search` there is no Settings row,
  endpoint or status line. The tool is in the Tools grid in both clients. Revisit if the user wants an install button.

## Done (tool 2026-11-23; install source 2026-11-24)

- [x] `nexus/firecrawl.py`, `nexus/tools/firecrawl.py`, `nexus firecrawl init|status`, `nexus doctor` row, permission key,
  side questions and forks allowed, shutdown stops the process.
- [x] Both clients: Tools grid chip, `Firecrawl` timeline label and argument, web icon, `page` activity noun (TUI and NSH
  edited together; the manifest note regenerated into `nexus-nsh/src/gen/manifest.ts`).
- [x] 14 tests with a stand-in server (`tests/test_firecrawl.py`); Core suite and `cargo test` pass.
- [x] Report Firecrawl startup diagnostics and remove reverse DNS from the stand-in HTTP server's bind path; 16 focused
  tests pass on local macOS. Hosted confirmation is tracked in the [0.15.0 handoff](../reports/release-0.15.0-validation.md).
- [x] Live run of the crates.io build: example.com scrape, crawl and map; an arXiv PDF as Markdown; docs.firecrawl.dev map;
  `127.0.0.1`, `localhost` refused; a 404 reported as an error; no process left behind.
- [x] External release install (2026-11-24): `install.sh` downloads the pinned asset and that release's `SHA256SUMS` for
  release and checkout installs, ignores a legacy Nexus asset, warns and continues without the tool when the release or
  platform asset is missing, and stops on a checksum mismatch. `scripts/test-install.sh` covers all four paths offline.

## Not verified

- [ ] A page that needs JavaScript (the Chrome path). The sandboxed Chrome on macOS and Linux, and `FIRECRAWL_CHROME_PATH`
  for a Chrome in a non-standard place, are untested.
- [ ] Linux and a machine with no Chrome.
- [ ] NSH: the test runner (`nsh`) is not installed here, so NSH's settings and timeline edits are checked by reading only.
  Paired TUI/NSH screenshots of the Tools grid and a `firecrawl` row are not captured.
- [ ] Whether a model picks `firecrawl` over `web_fetch` sensibly; look at real sessions after a week.
- [x] **Hosted dry run (old Nexus-asset path).** The [artifact workflow](https://github.com/nottherealsanta/nexus/actions/runs/45168609035)
  built and ran `--version` on Firecrawl Local for Linux x86_64 and arm64 and macOS x86_64 and arm64. Downloaded
  binaries matched their named architectures. `scripts/test-install.sh` covers the installer offline.
- [ ] The external `v0.1.1` release itself. The public repository exists, but the GitHub API listed no releases when
  checked on 2026-11-24, so a real install currently warns and leaves the tool unavailable. Verify the assets and their
  `SHA256SUMS`, then a live scrape; also try a downloaded macOS binary for quarantine or signing needs.
- [ ] A missing external release on a real install: it should warn and finish without `firecrawl-local`, and the tool
  should report that it is not installed (the offline installer test covers the installer half).
- [x] AGPL-3.0: the user approved installing the unmodified Firecrawl Local 0.1.1 binaries and
  `THIRD-PARTY/firecrawl-local.md` records the licence, pinned version and where the source is. Nexus talks to the binary
  over HTTP as a separate program and no longer redistributes it in Nexus release assets (2026-11-24).
- [ ] Upgrades: the version is pinned in `nexus/firecrawl.py` (`VERSION`) and in `install.sh` (`FC_VERSION`), and the
  external release tag carries it. Moving it means updating all three and testing the download and checksum; it is a
  manual change.
