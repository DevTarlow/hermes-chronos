"""Hermes dashboard/Desktop backend for Hermes Chronos.

Hermes imports this file by path and mounts its ``router`` under
``/api/plugins/hermes-chronos/``.  The plugin uses its own engine and route
builder; loading them by path keeps this module independent of the import
namespace Hermes chooses for the plugin directory.
"""

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_VERSION = "1.0.1"


def _load_module(name: str, path: Path):
    """Load one plain module by path without touching ``sys.modules``."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_engine_module = _load_module("hermes_chronos_engine_runtime", _PLUGIN_ROOT / "engine.py")
_api_module = _load_module("hermes_chronos_api_runtime", _PLUGIN_ROOT / "api.py")


def _settings() -> Dict[str, Any]:
    """Read ``plugins.entries.hermes-chronos.settings`` from the active profile."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        plugins = config.get("plugins") if isinstance(config, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("hermes-chronos") if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return {}
        settings = entry.get("settings")
        if isinstance(settings, dict):
            return settings
        legacy = entry.get("config")
        return legacy if isinstance(legacy, dict) else {}
    except Exception:
        logger.debug("Chronos could not read plugin settings", exc_info=True)
        return {}


def _default_cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:
        return str(Path.cwd())


router = _api_module.build_router(
    _engine_module.get_engine,
    settings_loader=_settings,
    default_cwd=_default_cwd,
    version=_VERSION,
)

__all__ = ["router"]
