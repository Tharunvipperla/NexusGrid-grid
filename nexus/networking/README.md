# networking — talking to other nodes

## What this owns

Everything that crosses the process boundary to another node (peer or relay):

- **discovery.py** — UDP broadcast on port 34567 (opt-out via settings).
- **peer_protocol.py** — the non-HTTP state machine for join / callback /
  rotate-tokens / revoke. The HTTP shell lives in `api.peer`; the *logic* lives
  here so it stays testable without FastAPI.
- **worker_client.py** — the long-lived Worker→Master WebSocket loop that
  registers capabilities, heartbeats, pulls tasks, and submits results.
- **relay_client.py** — the Relay WebSocket loop (register, heartbeat, message
  dispatch, relay↔peer HTTP bridging).
- **peer_http.py** — direct HTTP POST to a peer with relay fallback.
- **websocket_client.py** — version-tolerant WS client used by the worker loop.
- **connection_manager.py** — `ws_manager` registry of open peer WebSockets.
- **gossip.py** — periodic peer roster broadcast to trusted peers.
- **tunnel.py** (+) — TCP-over-WS service tunnel
  with master-side `127.0.0.1:N` listeners, per-tunnel rate limit,
  optional TLS termination, optional `protocol: "udp"` datagram path,
  optional 1 MB session-replay ring, and optional `shared_tunnel`
  ACL widening.
- **storage_pump.py** — encrypted-deposit chunk pump for
  foreign storage. Eleven `storage_*` frame types; AES-GCM 8 KB
  plaintext chunks; per-chunk ack with 30 s timeout; central
  `dispatch_storage_frame` routes inbound frames to the chunk
  receiver or the workflow handler installed at boot by
  `nexus.runtime.foreign_storage_workflow`.
- **storage_throttle.py** — token-bucket bandwidth limiter
  with busy / idle profiles plus a RAM-pressure pause check, so
  foreign-storage transfers never starve running tasks.

Peer workspace fetching (`resolve_p2p_cache`) lives in
:mod:`nexus.runtime.workspace` because it writes into the per-task runtime
workspace directory.

## Public surface

Exports from `nexus.networking`:

- `start_discovery(bind_host, discovery_port)` — launches the UDP listener.
- `start_worker_client()` + `master_manager_loop()` — worker loop (one per
  trusted master).
- `relay_client_loop()` — long-lived relay reconnect loop; driven from
  `nexus.app`'s lifespan.
- `gossip_broadcaster_loop()` — periodic peer roster broadcaster.
- `ws_manager` / `ConnectionManager` — peer WebSocket registry.
- `open_worker_websocket(...)` — version-tolerant WS client used by the worker
  loop.
- `peer_http_post(ip, path, body)` — direct-HTTP POST with relay fallback.
- `relay_http_request(target, method, path, body)` — HTTP-over-relay tunnel.
- `relay_send`, `relay_send_to_peer`, `get_relay_url`, `get_grid_key`,
  `set_relay_cli_overrides`, `set_grid_key_provider`.
- Peer-protocol helpers: `sign_join_request`, `verify_join_hmac`,
  `check_join_rate_limit`.
- `get_connected_master_peers()` — `Peer`-table query returning the trusted
  master + dual-role list.
- Tunnel (+): `ensure_local_listener`,
  `ensure_local_listeners`, `ensure_local_udp_listener`,
  `close_local_listener`, `close_local_udp_listener`, `reroute_tunnel`,
  + frame builders / handlers for both the TCP and UDP paths.
- Storage pump : eleven frame builders
  (`build_storage_offer`, `_offer_response`, `_chunk`, `_chunk_ack`,
  `_complete`, `_eviction_request`, `_eviction_response`,
  `_retrieve_open`, `_retrieve_chunk`, `_delete_now`, `_forward_init`),
  plus `transfer_deposit`, `receive_chunk`, `dispatch_storage_frame`.
- Storage throttle : `StorageThrottle`, `get_storage_throttle`,
  `install_storage_throttle`.

Workspace fetching (`resolve_p2p_cache`) lives under `nexus.runtime` because it
writes to the runtime workspace directory.

## Dependencies

- Imports from: `nexus.core`, `nexus.storage`, `nexus.security`, `nexus.tasks`,
  `nexus.scheduler`, `nexus.runtime`, `nexus.telemetry`.
- Imported by: `api`, `app` (lifespan starts the loops).

Forbidden: `ui` (the UI is not allowed to drive network state directly).

## Extending

- **New peer message type**: add it in `peer_protocol.py` and version the frame
  format. Never add a new message type to the worker-client / relay-client
  unless it is also understood by other nodes running the previous build.
- **New transport** (e.g. the TCP tunnel): add a new file in this
  package and wire it through `start_*` helpers.
- **Backward-compat**: peer protocol changes must go through the grid-key HMAC
  handshake; never broaden what an unauthenticated caller can do.

## Key files

| File                  | Purpose                                                |
|-----------------------|--------------------------------------------------------|
| `discovery.py`        | UDP broadcast discovery + discovered-peer table        |
| `peer_protocol.py`    | Join / callback / rotate / revoke state machine        |
| `connection_manager.py`| Peer WebSocket registry (`ws_manager`)                |
| `websocket_client.py` | Version-tolerant WS client (worker → master handshake) |
| `worker_client.py`    | Worker → Master WebSocket loop + `master_manager_loop` |
| `relay_client.py`     | Relay connection, HTTP-over-relay bridging             |
| `peer_http.py`        | Direct HTTP POST to a peer with relay fallback         |
| `gossip.py`           | Periodic peer roster broadcaster                       |
| `tunnel.py` | /5a TCP+UDP service tunnels, replay, rate-limit |
| `storage_pump.py` | encrypted-deposit chunk pump + frame builders |
| `storage_throttle.py` | busy/idle bandwidth bucket for foreign storage |
| `log_forwarder.py`    | Worker → master live log forwarding                    |

Peer-workspace fetching (`resolve_p2p_cache`) lives under `nexus.runtime`
because it writes to the runtime workspace directory.
