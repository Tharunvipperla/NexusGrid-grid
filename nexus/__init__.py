"""NexusGrid — modular peer-to-peer distributed compute grid.

Public top-level surface:
    - create_app: FastAPI application factory (see nexus.app)
    - __version__: package version string

Everything else lives under the submodules. Importing from `nexus` directly should
be rare — prefer `from nexus.<subpackage> import ...`.
"""

__version__ = "1.1.0"

# `create_app` is exposed lazily to keep `import nexus` cheap (no FastAPI import
# cost until someone actually wants the app).
def create_app(*args, **kwargs):
    from nexus.app import create_app as _create_app
    return _create_app(*args, **kwargs)


__all__ = ["create_app", "__version__"]
