# nexus/sdk

A thin Python client and an OpenAPI-driven CLI for a NexusGrid node's local API
(feature **D2**). Everything is derived from the node's live `/openapi.json`, so
the command surface never drifts from the real API. See the
[API & SDK dev guide](../../docs/dev/api-and-sdk.md).

| Module | Purpose |
|---|---|
| `client.py` | `NexusClient` — a minimal token-attaching HTTP client. `NexusClient.from_local(base)` auto-discovers the token from `.nexus_local_token`. |
| `openapi.py` | OpenAPI helpers: fetch + flatten the live spec into a list of operations. |
| `cli.py` | The CLI: `ops` (list operations) and `call METHOD PATH` (invoke one). |
| `__main__.py` | Entry point so `python -m nexus.sdk …` works. |

```bash
python -m nexus.sdk --base https://127.0.0.1:8000 ops
python -m nexus.sdk --base https://127.0.0.1:8000 call GET /local/network
```
```python
from nexus.sdk import NexusClient
print(NexusClient.from_local("https://127.0.0.1:8000").get("/local/network"))
```

Nothing heavy is bundled — to generate a fully-typed client, point a standard
generator (e.g. `openapi-typescript`, `openapi-python-client`) at the live spec.
