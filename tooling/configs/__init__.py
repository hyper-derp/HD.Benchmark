"""Platform / mode configuration registry.

Each platform declares its relay + clients + tunnel topology and
the absolute paths to the daemon binaries on the deployed hosts.
Modes look these up by name via `get_platform()`.
"""

from .platforms import cloud_gcp_c4, bare_metal_mellanox

_PLATFORMS = {
    cloud_gcp_c4.NAME: cloud_gcp_c4,
    bare_metal_mellanox.NAME: bare_metal_mellanox,
}


def get_platform(name):
  """Return the platform module by name, or raise KeyError."""
  if name not in _PLATFORMS:
    raise KeyError(
        f"unknown platform {name!r}; "
        f"known: {sorted(_PLATFORMS)}")
  return _PLATFORMS[name]


def known_platforms():
  """List of platform names."""
  return sorted(_PLATFORMS)
