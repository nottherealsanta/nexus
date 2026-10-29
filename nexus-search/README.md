# Nexus Search

Local SearXNG deployment for the planned Core `web_search` tool. Adapted from
`../nexus/websearch/`; Core integration is tracked in PLAN.md Step 11a.
Default endpoint: `http://127.0.0.1:18765/search`.

Run either Docker or Apple container, not both on the same port. The Docker
Compose path was smoke-tested on 2026-10-29: the container starts and
`/search?format=json` returns results. The Apple `container` path has not been
tried here. `latest` is a bootstrap default; the tested image was
`searxng/searxng@sha256:eb2a404db174d33a04cd9f2e519bcae2ca722a58923510652cc1314be94e8e63`
(created 2026-09-29). Set `SEARXNG_IMAGE` to that digest, or a newer tested one,
before release (PLAN.md O6).

## Configuration

From this directory, generate a private secret (once):

```sh
umask 077
printf 'SEARXNG_SECRET=%s\n' "$(openssl rand -hex 32)" > .env
```

Keep `.env` private and in place; it is ignored by Git. The mounted `searxng/`
directory retains generated configuration. Only `settings.yml` is tracked.
Optionally add `SEARXNG_IMAGE=searxng/searxng:<tested-tag>` to `.env`.

## Docker

With Docker Engine running:

```sh
docker compose up -d
docker compose logs --tail 100 searxng
```

Stop/remove the container (retaining local configuration):

```sh
docker compose down
```

## Apple container

Requires Apple's `container` CLI and its supported macOS/Apple silicon host.
Start its service and the same image/configuration:

```sh
container system start
set -a
. ./.env
set +a
container run --detach --name nexus-search \
  --publish 127.0.0.1:18765:8080 \
  --env SEARXNG_BASE_URL=http://127.0.0.1:18765/ \
  --env "SEARXNG_SECRET=$SEARXNG_SECRET" \
  --volume "$PWD/searxng:/etc/searxng" \
  "${SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}"
container logs nexus-search
```

Stop/remove:

```sh
container stop nexus-search
container delete nexus-search
```

Apple command reference: https://github.com/apple/container/blob/main/docs/command-reference.md

## Health check (either runtime)

```sh
curl -fsS http://127.0.0.1:18765/ -o /dev/null
curl -fsS 'http://127.0.0.1:18765/search?q=nexus&format=json'
```

Core should report an actionable error if unavailable, rather than silently
installing or launching a container runtime. `web_fetch` uses HTTP directly;
it does not depend on SearXNG or browser-use.
