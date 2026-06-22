# deploy/

Deployment artifacts — container images, compose files, and anything else for
*running* NexusGrid pieces as a service. Keeps the repo root from filling up with
`Dockerfile.*` / compose files as more deploy targets are added; new deploy files
go here.

| File | What it is |
|---|---|
| `Dockerfile.relay` | Standalone relay-server image. |
| `docker-compose.relay.yml` | One-command relay deployment. |

## Building (run from the repository root)
The relay image needs `nexus/relay/server.py` (the bundled relay source). That's
why the **build context is the repo root**, not this folder:

```sh
NEXUS_GRID_KEY=your-secret docker compose -f deploy/docker-compose.relay.yml up -d
# or:
docker build -f deploy/Dockerfile.relay -t nexus-relay .
```

Full walkthrough: [`../docs/guides/relay-deploy.md`](../docs/guides/relay-deploy.md).
