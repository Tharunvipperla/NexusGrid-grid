# Database-as-a-Service (DBaaS)

DBaaS is **not a new subsystem** — it's the existing service marketplace
(Waves 53–60) plus a thin, pluggable *provider-adapter* layer. You already run a
database; NexusGrid provisions a private per-consumer database + login on it,
tunnels the wire protocol to the consumer, meters it, and drops it on revoke.
The philosophy is the same as relays/pumps/runners: **we ship the enablement;
the nodes do the work.**

## How it fits together

```
consumer psql ──▶ 127.0.0.1:<localport>  ──(byte tunnel over relay)──▶  host 127.0.0.1:5432 (your Postgres)
                         ▲                                                          ▲
                  grant-gated, metered                              per-consumer DB+role provisioned
                                                                    by your db_provider adapter
```

- **Data plane:** the `service_tunnel` already forwards raw TCP — Postgres,
  MySQL, Redis, Mongo all just flow. Nothing DB-specific there.
- **Control plane:** on an *approved* `ServiceGrant`, when the consumer fetches
  credentials the host's adapter runs `CREATE DATABASE`/`CREATE ROLE` (idempotent),
  records it in `service_db_provisions`, and returns the login. On **revoke** the
  adapter drops the database + role.

## Hosting a database service

1. Run your engine locally (e.g. Postgres on `127.0.0.1:5432`).
2. Install a driver the adapter needs (`pip install psycopg` for Postgres).
3. In **Services → Deploy a service**, fill the **Database service** block:
   - **Service kind**: `postgres` (drives the consumer's connection string)
   - **Provider engine**: `postgres` (which adapter provisions)
   - **Admin DSN**: `postgresql://admin:pw@127.0.0.1:5432/postgres` — **host-only;
     stripped before the service is advertised** (it holds your admin secret).
   - **Local host/port**: `127.0.0.1` / `5432` (where the engine listens).
   - **Access**: `permission` (approve each consumer) or `free`.

That's it. When you approve a consumer and they connect, they get a dedicated
`nx_<hash>` database + login — isolated per consumer, revocable.

## Using a database service (consumer)

1. **Discover** the service and **Request access**; wait for approval.
2. In **My access**: **Connect** (opens the local tunnel) → **DB credentials**
   (fetches your provisioned `database` / `user` / `password`).
3. Copy the ready connection string, e.g.
   `PGPASSWORD=… psql -h localhost -p <localport> -U nx_… nx_…`.

Credentials are fetched on demand and never stored on the consumer's disk.

## Adding another engine (cookbook)

A DB-provider adapter is any `nexus_dbproviders/<engine>.py` next to the node
exposing `create` / `drop` (and optional `KIND`). Adapters run **your own code
on your own machine** against your own engine. Template (MySQL):

```python
# nexus_dbproviders/mysql.py  — drop in next to the node, then set
# Provider engine: mysql  /  Service kind: mysql
KIND = "mysql"

def create(admin_dsn, database, user, password):
    import pymysql  # your driver
    conn = pymysql.connect(... parse admin_dsn ...); conn.autocommit(True)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (user, password))
        cur.execute(f"GRANT ALL ON `{database}`.* TO %s@'%%'", (user,))
    conn.close()

def drop(admin_dsn, database, user):
    import pymysql
    conn = pymysql.connect(...); conn.autocommit(True)
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
        cur.execute("DROP USER IF EXISTS %s@'%%'", (user,))
    conn.close()
```

`database` and `user` are framework-generated, deterministic `nx_<16-hex>`
identifiers (safe by construction). Keep `create`/`drop` **idempotent**.

## Security model

- **admin_dsn never leaves the host** — it's in `_SERVICE_PRIVATE_FIELDS`,
  stripped by `public_services`.
- Per-consumer **least-privilege** login; **revocable** (drops the DB+role and
  cuts live tunnels at once).
- No SSRF — the provider only ever dials its own configured `local_host:local_port`.
- The host DB is plaintext to the host (it's the host's own engine), like the
  view-grant model — be explicit with consumers about what a DB host can see.
- Credential fetch is authenticated by the same signed-statement model as the
  access request, and only served for an `approved` grant.

## Not in v1 (fast-follow)

- One-click **engine bring-up** via the sandbox runners (today: point at an
  engine you already run).
- Additional bundled reference adapters (MySQL/Redis/Mongo) — clone the cookbook
  above for now.

## Canonical code

`nexus/runtime/db_provider.py` (adapters), `nexus/runtime/service_grants.py`
(provision/deprovision + `/peer/service_db_credentials`), `nexus/api/local.py`
(`/local/service_grants/{id}/db_credentials`), `nexus/core/config.py`
(`service_kind` + host-only `db_provider`). Tests: `tests/test_wave71_dbaas.py`.
