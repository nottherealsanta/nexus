# firecrawl-local

Nexus releases include the `firecrawl-local-<os>-<arch>` binaries, which `install.sh` puts in `$PREFIX/bin/firecrawl-local`
and the `firecrawl` tool runs.

- **What it is:** Firecrawl Local, by Mendable AI, Inc. (Firecrawl), <https://github.com/firecrawl/firecrawl>, in `apps/local`.
- **Licence:** GNU Affero General Public License, version 3 (AGPL-3.0). The full text is
  <https://www.gnu.org/licenses/agpl-3.0.txt> and `LICENSE` at the root of that repository.
- **Version:** the one pinned as `VERSION` in `nexus-core/nexus/firecrawl.py`, built unmodified from the crate of the same
  name and version on crates.io (`cargo install firecrawl-local --version <VERSION> --locked`) by the release workflow.
- **Source:** the crate's source is at <https://crates.io/crates/firecrawl-local> (the "source" link on each version) and in
  the repository above at the matching `apps/local` revision. Nexus changes nothing in it.
- **How Nexus uses it:** as a separate program started as a child process that talks to Nexus over HTTP on a loopback
  address. Nexus does not link it or include its code. Nexus's own licence is not affected by this arrangement, and this
  notice is not legal advice.
