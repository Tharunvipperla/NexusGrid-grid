"""Entry point for `python -m nexus` and the PyInstaller executable.

Responsibilities (kept deliberately thin):
    1. Parse CLI args via `nexus.cli`.
    2. Build the FastAPI app via `nexus.app.create_app`.
    3. Print a user-friendly URL and (unless --no-browser) open it.
    4. Hand off to uvicorn.
"""

from __future__ import annotations

import threading
import webbrowser


def _open_browser_when_ready(url: str, delay: float = 1.2) -> None:
    """Open *url* after a short delay so uvicorn is listening first."""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def _prescan_data_dir() -> None:
    """Honor ``--data-dir`` / ``--data-dir=…`` before any nexus import.

    ``nexus.core.paths.BASE_DIR`` is resolved at import time from
    ``NEXUS_DATA_DIR``, so the flag has to land in the environment first — the
    real argparse pass (which imports nexus) runs afterwards for help/validation.
    """
    import os
    import sys

    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--data-dir" and i + 1 < len(argv):
            os.environ["NEXUS_DATA_DIR"] = argv[i + 1]
        elif a.startswith("--data-dir="):
            os.environ["NEXUS_DATA_DIR"] = a.split("=", 1)[1]


def main() -> None:
    _prescan_data_dir()

    import uvicorn

    from nexus.app import create_app
    from nexus.cli import parse_args
    from nexus.security.limits import get_max_ws_frame_bytes
    from nexus.security.tls import ensure_local_cert

    args = parse_args()
    app = create_app(args)

    tls_kwargs: dict = {}
    scheme = "http"
    if not args.no_tls:
        try:
            cert, key = ensure_local_cert()
            tls_kwargs = {"ssl_keyfile": str(key), "ssl_certfile": str(cert)}
            scheme = "https"
        except Exception as exc:
            print(f"[nexus] TLS setup failed ({exc}); falling back to HTTP.")

    # `0.0.0.0` is a bind address, not something a browser can visit. Pick a
    # host the user can actually click on for the banner + auto-launch.
    visit_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"{scheme}://{visit_host}:{args.port}/"
    print(f"[nexus] UI:    {url}")
    print(f"[nexus] Bound: {scheme}://{args.host}:{args.port} (listen address)")

    if not args.no_browser:
        _open_browser_when_ready(url)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        ws_max_size=get_max_ws_frame_bytes(),
        **tls_kwargs,
    )


if __name__ == "__main__":
    main()
