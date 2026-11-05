"""Tool handlers for Hermes Chronos.

Two surfaces live here:

* :class:`ChronosTools` builds the ``(args, **kwargs) -> JSON string`` handlers
  that are registered with ``ctx.register_tool``.
* Module-level ``chronos_*`` functions keep the simpler
  ``(ctx, ...) -> JSON string`` shape used by the original design notes and by
  direct callers/tests.  They are not registered with Hermes; the class methods
  are, because Hermes injects session/task context into handlers.
"""

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from .engine import get_engine as _cached_engine, resolve_workspace

logger = logging.getLogger(__name__)

MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 15
MAX_DIFF_CHARS = 1_000_000
MIN_DIFF_CHARS = 2_000

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n"}

# Best-effort turn ordinal per session.  Hermes gives hooks a UUID ``turn_id``
# rather than a numeric turn index; remembering the order in which this plugin
# first saw each turn lets the timeline label them 1, 2, 3, ...
_TURN_SEEN: Dict[str, List[str]] = {}
_TURN_LOCK = threading.Lock()
_MAX_TRACKED_SESSIONS = 500
_MAX_TRACKED_TURNS = 200


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _config(ctx: Any, key: str, default: Any) -> Any:
    """Read a plugin setting through the real PluginContext or a test double.

    ``ctx.get_config`` is the supported API on current Hermes.  Hermes 0.19
    predates it, so the fallback reads the same public
    ``plugins.entries.<id>.settings`` subtree through ``hermes_cli.config``.
    """
    if ctx is None:
        return _config_from_home(key, default)
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            logger.debug("Chronos could not read config %r", key, exc_info=True)
            return default
    config = getattr(ctx, "config", None)
    if isinstance(config, dict):
        return config.get(key, default)
    declared = getattr(ctx, "plugin_config", None)
    if isinstance(declared, dict):
        return declared.get(key, default)
    return _config_from_home(key, default)


def _config_from_home(key: str, default: Any) -> Any:
    """Read this plugin's settings from the active profile's config.yaml."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        plugins = config.get("plugins") if isinstance(config, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("hermes-chronos") if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return default
        settings = entry.get("settings")
        if isinstance(settings, dict) and key in settings:
            return settings[key]
        legacy = entry.get("config")
        if isinstance(legacy, dict) and key in legacy:
            return legacy[key]
    except Exception:
        logger.debug("Chronos could not read %r from config.yaml", key, exc_info=True)
    return default


def _ignored_patterns(ctx: Any) -> Optional[List[str]]:
    raw = _config(ctx, "ignored_patterns", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = [part.strip() for part in value.split(",")]
        raw = parsed
    if isinstance(raw, (list, tuple, set)):
        patterns = [str(item).strip() for item in raw if str(item).strip()]
        return patterns or None
    return None


def _workspace_for(ctx: Any, kwargs: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the workspace; internal kwargs allow tests/API to pin it."""
    explicit = None
    if kwargs:
        explicit = kwargs.get("workspace") or kwargs.get("cwd") or kwargs.get("_workspace")
    if explicit:
        return str(explicit)
    declared = getattr(ctx, "workspace_dir", None) if ctx is not None else None
    if declared:
        return str(declared)
    return resolve_workspace()


def turn_index_for(session_id: str = "", task_id: str = "", turn_id: str = "") -> Optional[int]:
    """Return a stable 1-based ordinal for ``turn_id`` within a session."""
    turn_id = str(turn_id or "").strip()
    if not turn_id:
        return None
    key = str(session_id or task_id or "default")
    with _TURN_LOCK:
        if len(_TURN_SEEN) > _MAX_TRACKED_SESSIONS and key not in _TURN_SEEN:
            _TURN_SEEN.clear()
        seen = _TURN_SEEN.setdefault(key, [])
        if turn_id not in seen:
            seen.append(turn_id)
            if len(seen) > _MAX_TRACKED_TURNS:
                del seen[: _MAX_TRACKED_TURNS // 2]
        return seen.index(turn_id) + 1


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return _json({"status": "error", "error": message})


# ---------------------------------------------------------------------------
# Real Hermes handlers
# ---------------------------------------------------------------------------
class ChronosTools:
    """Config-aware tool handlers bound to one PluginContext."""

    def __init__(self, ctx: Any):
        self.ctx = ctx

    # -- engine ---------------------------------------------------------
    def _engine(self, kwargs: Optional[Dict[str, Any]] = None):
        workspace = _workspace_for(self.ctx, kwargs)
        max_checkpoints = _as_int(_config(self.ctx, "max_checkpoints", 50), 50)
        patterns = _ignored_patterns(self.ctx)
        return _cached_engine(workspace, max_checkpoints=max_checkpoints, ignored_patterns=patterns)

    # -- chronos_snapshot ----------------------------------------------
    def snapshot(self, args: Dict[str, Any], **kwargs: Any) -> str:
        """Create a checkpoint before a risky operation."""
        try:
            args = args if isinstance(args, dict) else {}
            description = str(args.get("description") or "").strip()
            if not description:
                description = "Agent checkpoint"
            tool_name = str(args.get("tool_name") or "agent_call").strip()[:80] or "agent_call"
            session_id = str(kwargs.get("session_id") or "")
            task_id = str(kwargs.get("task_id") or "")
            turn_id = str(kwargs.get("turn_id") or "")
            turn = turn_index_for(session_id, task_id, turn_id)
            engine = self._engine(kwargs)
            result = engine.create_checkpoint(
                description[:500],
                tool_name=tool_name,
                turn_index=turn,
                session_id=session_id,
                turn_id=turn_id,
                task_id=task_id,
            )
            return _json(result)
        except Exception as exc:
            logger.debug("chronos_snapshot failed", exc_info=True)
            return _error(exc)

    # -- chronos_rollback ----------------------------------------------
    def rollback(self, args: Dict[str, Any], **kwargs: Any) -> str:
        """Restore workspace files to a checkpoint."""
        try:
            args = args if isinstance(args, dict) else {}
            checkpoint_id = str(
                args.get("checkpoint_id") or args.get("id") or args.get("turn_or_id") or ""
            ).strip()
            if not checkpoint_id:
                return _json(
                    {
                        "status": "error",
                        "error": "checkpoint_id is required (use chronos_list_checkpoints to find one).",
                    }
                )
            clean = _as_bool(args.get("clean"), _as_bool(_config(self.ctx, "clean_untracked_on_rollback", True), True))
            # The model tool never skips the safety checkpoint: a rollback that
            # cannot be undone is exactly the failure mode Chronos exists to
            # prevent.  ``no_safety`` stays available to the human surfaces
            # (``/chronos --no-safety`` and the Desktop pane's create_safety).
            engine = self._engine(kwargs)
            return _json(engine.rollback(checkpoint_id, clean=clean, create_safety=True))
        except Exception as exc:
            logger.debug("chronos_rollback failed", exc_info=True)
            return _error(exc)

    # -- chronos_diff ----------------------------------------------------
    def diff(self, args: Dict[str, Any], **kwargs: Any) -> str:
        """Inspect file changes between checkpoints or the working tree."""
        try:
            args = args if isinstance(args, dict) else {}
            from_id = args.get("from_checkpoint") or args.get("from") or None
            to_id = args.get("to_checkpoint") or args.get("to") or "HEAD"
            checkpoint_id = args.get("checkpoint_id") or args.get("id")
            max_chars = _as_int(args.get("max_chars"), 400_000, MIN_DIFF_CHARS, MAX_DIFF_CHARS)

            engine = self._engine(kwargs)
            if checkpoint_id and not from_id and to_id in (None, "", "HEAD"):
                result = engine.get_checkpoint_diff(str(checkpoint_id), max_chars=max_chars)
            else:
                result = engine.get_diff(
                    str(from_id) if from_id else None,
                    str(to_id) if to_id else "HEAD",
                    max_chars=max_chars,
                )
            return _json(result)
        except Exception as exc:
            logger.debug("chronos_diff failed", exc_info=True)
            return _error(exc)

    # -- chronos_list_checkpoints --------------------------------------
    def list_checkpoints(self, args: Dict[str, Any], **kwargs: Any) -> str:
        """List recent checkpoints."""
        try:
            args = args if isinstance(args, dict) else {}
            limit = _as_int(args.get("limit"), DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT)
            engine = self._engine(kwargs)
            checkpoints = engine.list_checkpoints(limit=limit)
            return _json(
                {
                    "status": "ok",
                    "checkpoints": checkpoints,
                    "count": len(checkpoints),
                    "workspace": engine.workspace_root,
                }
            )
        except Exception as exc:
            logger.debug("chronos_list_checkpoints failed", exc_info=True)
            return _error(exc)


# ---------------------------------------------------------------------------
# Spec-compatible module-level helpers
# ---------------------------------------------------------------------------
def get_engine(ctx: Any = None, workspace: Optional[str] = None):
    """Build a config-aware engine for ``ctx`` (or the current workspace)."""
    root = str(workspace) if workspace else _workspace_for(ctx)
    max_checkpoints = _as_int(_config(ctx, "max_checkpoints", 50), 50)
    patterns = _ignored_patterns(ctx)
    return _cached_engine(root, max_checkpoints=max_checkpoints, ignored_patterns=patterns)


def chronos_snapshot(ctx: Any, description: str, tool_name: Optional[str] = "agent_call") -> str:
    """Create a point-in-time snapshot before risky operations."""
    try:
        engine = get_engine(ctx)
        return _json(
            engine.create_checkpoint(
                str(description or "Agent checkpoint")[:500],
                tool_name=str(tool_name or "agent_call")[:80],
            )
        )
    except Exception as exc:
        return _error(exc)


def chronos_rollback(ctx: Any, checkpoint_id: str) -> str:
    """Restore the workspace to a specific snapshot ID."""
    try:
        engine = get_engine(ctx)
        clean = _as_bool(_config(ctx, "clean_untracked_on_rollback", True), True)
        return _json(engine.rollback(str(checkpoint_id or ""), clean=clean))
    except Exception as exc:
        return _error(exc)


def chronos_diff(
    ctx: Any,
    from_checkpoint: Optional[str] = None,
    to_checkpoint: Optional[str] = "HEAD",
) -> str:
    """Return the diff between two checkpoints or since the last checkpoint."""
    try:
        engine = get_engine(ctx)
        return _json(engine.get_diff(from_checkpoint, to_checkpoint or "HEAD"))
    except Exception as exc:
        return _error(exc)


def chronos_list_checkpoints(ctx: Any, limit: int = DEFAULT_LIST_LIMIT) -> str:
    """List recent checkpoints with IDs, timestamps, and triggering tools."""
    try:
        engine = get_engine(ctx)
        checkpoints = engine.list_checkpoints(limit=_as_int(limit, DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT))
        return _json({"status": "ok", "checkpoints": checkpoints, "count": len(checkpoints)})
    except Exception as exc:
        return _error(exc)


__all__ = [
    "ChronosTools",
    "chronos_diff",
    "chronos_list_checkpoints",
    "chronos_rollback",
    "chronos_snapshot",
    "get_engine",
    "turn_index_for",
]
