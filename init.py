"""Compatibility alias for environments that look for ``init.py``.

Hermes directory plugins are loaded from ``__init__.py`` (see
``PluginManager._load_directory_module``).  This module exists only so tools
that expect the historical ``init.py`` name can still find the entrypoint.
"""

from . import register

__all__ = ["register"]
