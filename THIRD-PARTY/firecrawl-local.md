# firecrawl-local

`install.sh` installs `firecrawl-local` (the scraper behind the `firecrawl` tool) into `$PREFIX/bin/firecrawl-local` from
its own public release, [nottherealsanta/firecrawl-local-builds](https://github.com/nottherealsanta/firecrawl-local-builds):
the pinned `v0.1.1` asset `firecrawl-local-<os>-<arch>`, checked against that release's `SHA256SUMS` before anything is
installed. Nexus does not build it in its own CI. A missing release or platform leaves the tool unavailable (the installer
warns and continues); a checksum mismatch stops the install.

- **What it is:** Firecrawl Local, by Mendable AI, Inc. (Firecrawl), <https://github.com/firecrawl/firecrawl>, in `apps/local`.
- **Licence:** GNU Affero General Public License, version 3 (AGPL-3.0). The full text is
  <https://www.gnu.org/licenses/agpl-3.0.txt> and `LICENSE` at the root of that repository.
- **Version:** 0.1.1, pinned in `install.sh`, unmodified from crates.io; the external build repository builds the crate of
  the same name and version (`cargo install firecrawl-local --version 0.1.1 --locked`).
- **Source:** the crate's source is at <https://crates.io/crates/firecrawl-local> (the "source" link on the version) and in
  the repository above at the matching `apps/local` revision. Nexus changes nothing in it.
- **How Nexus uses it:** as a separate program started as a child process that talks to Nexus over HTTP on a loopback
  address. Nexus does not link it or include its code. Nexus's own licence is not affected by this arrangement, and this
  notice is not legal advice.
