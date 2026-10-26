# Nexus (NSH app)

A basic Nexus web client written as a Native Shell (NSH) Rust app: sessions
sidebar, timeline (messages, labelled tool rows, turn errors) and a composer.
It talks to the daemon's `/v1/web/*` host contract with relative URLs
(`../v1/web/...`), so it must be served by the daemon at `<prefix>/app/` on the
same origin as the API. It polls (selected session every 2s, session list every
5s); there is no SSE yet.

JSON-to-row conversion is in `src/model.rs` and is unit tested: `cargo test`.

Build and try it:

```sh
scripts/nsh-app.sh package web   # from the repo root; or: cd rust/app && nsh package --target web
nexus web                        # then open /w/<key>/app/ on the same origin
```

If the page says "Open `nexus web` first, then reload /app/", the browser has no
session cookie yet: open the normal `nexus web` page once.
