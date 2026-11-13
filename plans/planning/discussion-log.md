# Discussion log: nexus.santa.build install site

Newest last. This is the record of what was asked and answered, so we can come back to it. The working draft is
[install-site.md](install-site.md). Decisions are marked **(user)**; everything else is the agent's proposal and still open.

## 2026-11-12, first request (voice)

The user wants the installer served from `nexus.santa.build`: `/install.sh` for people to install Nexus, and the same site carries
docs, for example how to install the iOS app on your own phone. `/install.sh` should point at the GitHub one. They believed CI
already shows it works on every system. They asked for questions and "a very good plan".

### What the agent found

- `install.sh` is attached to each GitHub release and reads its download base from `BASE`, not from where it was fetched, so it needs no
  change to be reached through another URL.
- CI only runs `sh -n install.sh` and an offline, stubbed `scripts/test-install.sh` on one Linux runner. The four TUI binaries are
  built and smoke-tested with `--help`. The only **real** published install was macOS arm64 (0.12.0, 11-09). So "CI covers every system"
  is only partly true: Linux x86_64, Linux arm64 and macOS Intel have not run the installer end to end.
- The release has no web bundle (the job is `if: false`), so docs must not promise `nexus web`.
- Right after a release, `releases/latest/download/install.sh` 404s until the build and upload finish (up to about 45 minutes).

### Answers **(user)**

| Question | Answer |
| --- | --- |
| Hosting | Look at `../santa.build`; it deploys to `build-box`. "Tell me if Cloudflare Pages will be a better option." |
| `/install.sh` mode | 302 redirect to GitHub |
| Where the site source lives | A separate repo |
| iOS page | Build-it-yourself only |
| Day-one extras | Version pinning, uninstall and update docs, a read-the-script-first option. No Windows page. |

### Agent's findings on hosting (`../santa.build`)

- Everything runs as one container per project on the `app` VM, behind a Cloudflare tunnel. Deploys go through a shared workflow on a
  self-hosted runner. `santa.build/AGENTS.md` says "No Pages or Workers".
- A **public** repo must never use the self-hosted runner. `experiments` shows the pattern: the public repo only sends a
  `repository_dispatch`; the private `santa.build` repo builds and deploys it.
- Recommendation: stay on `build-box`, and add a Cloudflare **Redirect Rule** for only the install paths so `/install.sh` keeps
  working when the NUC is down. Use Pages only if the user accepts breaking the "no Pages" rule for the sake of uptime.

## 2026-11-12, second message (voice)

The user asked two things.

1. **"Should it be a different site? Do we have more security if it's a different site? Not sure?"**
   Agent's answer: a separate subdomain and repo is better than a path on `santa.build` (own origin, public dispatch-only repo, tiny
   surface), but it is **not safer than linking GitHub directly**: it adds one more party who could change what people run, and the
   checksums protect the payloads, not the script. Mitigations listed in the draft: account 2FA, an edge redirect with a hard-coded
   target, branch protection, a test that fails on any other redirect host, hostile-path tests, and an optional build-provenance
   attestation for `install.sh`.
2. **The installer should install `uv`**, because Core, the Monty kernel and the rest are Python. **(user)**
   Agent's design: pinned Astral installer, no shell-profile edits, full path to `uv` for the rest of the run, a printed notice,
   `--no-uv` to opt out, an existing `uv` is untouched, uninstall leaves `uv`, new offline tests. A missing C compiler (for the
   `claude-agent-sdk` source build) is a separate problem.

## 2026-11-12, third message (voice)

The user asked to put the discussion in a folder called `planning` inside `plans/`, and said "I think we need to discuss more before
we make it concrete." Moved the draft to `plans/planning/install-site.md`, marked it as a draft and not a numbered plan, and
started this log. Nothing was built and no other repo was touched.

## Still to discuss

These are the open points, in the order they seem to matter. None is decided.

1. **Separate site or not, more carefully.** The user was not sure. Options: (a) a separate site as drafted; (b) only the redirect at
   the Cloudflare edge plus a docs section on the main homepage, with no new repo or container; (c) skip the site and put docs in
   this repo's README and `docs/` only, linking GitHub directly. Option (b) or (c) has the smallest trust surface. The user
   previously chose "separate repo", then asked whether it is more secure; revisit with that in mind.
2. **Provenance attestation** for `install.sh` (touches `release.yml`, which is deferred CI work).
3. **`uv` install route:** Astral's pinned installer, or the pinned release tarball with its checksum verified.
4. **The 404 window after a release:** drafts until assets are attached, or accept it.
5. **Smoke workflow** for the four platforms: allowed despite "CI is deferred", or manual per release.
6. **Repo name** (default `nexus-site`).
7. **Free vs paid Apple account** on the iOS page: unknown whether the dev build was ever done on a free one.
8. **Cloudflare Pages vs `build-box`:** the agent recommends `build-box` plus the edge redirect; the user has not confirmed.
9. **Port** for the site (8084 proposed, unchecked against the registry).
