"""Cloud-eviction provider drivers.

Importing this package registers all four drivers in
:data:`nexus.storage.cloud.base.PROVIDERS`. Only the GDrive driver has a
real upload implementation; s3 / r2 / b2 stubs raise ``NotImplementedError``
and exist so the depositor UI can enumerate the eventual provider set.
"""

from nexus.storage.cloud.base import PROVIDERS, CloudProvider

# Side-effect imports register each provider in PROVIDERS.
from nexus.storage.cloud import b2 as _b2  # noqa: F401
from nexus.storage.cloud import gdrive as _gdrive  # noqa: F401
from nexus.storage.cloud import r2 as _r2  # noqa: F401
from nexus.storage.cloud import s3 as _s3  # noqa: F401

__all__ = ["CloudProvider", "PROVIDERS"]
