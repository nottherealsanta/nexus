# Local SearXNG for Nexus

This Compose project runs SearXNG on `127.0.0.1:18765`. The published port is
bound exclusively to host loopback; the service is not exposed on LAN interfaces.
The local search URL is `http://127.0.0.1:18765/search`.

Docker Engine must be running before using these commands. Port `18765` must be
free on the host.

## Start

From this directory, create a private `.env` file with a fresh secret, then start
the service:

```sh
umask 077
printf 'SEARXNG_SECRET=%s\n' "$(openssl rand -hex 32)" > .env
docker compose -f compose.yaml up -d
```

The `.env` file is ignored by Git. Keep it in place while the service is running;
Compose also reads it when stopping or inspecting the service. The SearXNG config
directory is mounted at `./searxng` and persists across container replacement;
only `settings.yml` is tracked, while other generated config files are ignored.

Check the HTML page and JSON search API:

```sh
curl -fsS http://127.0.0.1:18765/ -o /dev/null
curl -fsS 'http://127.0.0.1:18765/search?q=nexus&format=json'
```

Stop and remove the container with:

```sh
docker compose -f compose.yaml down
```

## Nexus

Nexus uses this local SearXNG instance by default at
`http://127.0.0.1:18765/search`. Start the Compose service before using Nexus
web search. Nexus's separate optional remote SearXNG configuration continues to
require HTTPS.
