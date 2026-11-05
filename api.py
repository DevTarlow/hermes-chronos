"""FastAPI routes for the Hermes Desktop Chronos pane.

Hermes mounts a plugin backend from ``dashboard/plugin_api.py`` under
``/api/plugins/<plugin-name>/``.  This module builds the router without
importing the engine at module import time, so the same code can be loaded by
path from ``dashboard/plugin_api.py`` (which supplies the engine factory) and
exercised directly in tests.

Endpoints
---------
``GET  /health``                 repository + settings health
``GET  /timeline``               checkpoint list (query parameters)
``POST /timeline``               checkpoint list (JSON body; used by Desktop)
``GET  /diff``                   diff by id / between checkpoints
``POST /diff``                   diff (JSON body; used by Desktop)
``GET  /checkpoint/{id}``        one checkpoint's metadata + diff
``POST /rollback``               restore a checkpoint
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Body, Query

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 1_000_000
MIN_DIFF_CHARS = 2_000
MAX_LIMIT = 200
DEFAULT_LIMIT = 30
_MAX_ERROR_CHARS = 2000

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "auto_snapshot": True,
    "max_checkpoints": 50,
    "ignored_patterns": None,
    "clean_untracked_on_rollback": True,
}


def _safe_error(exc: BaseException) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:_MAX_ERROR_CHARS]


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_settings(raw: Any) -> Dict[str, Any]:
    settings = dict(_DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return settings
    if "auto_snapshot" in raw:
        settings["auto_snapshot"] = _to_bool(raw.get("auto_snapshot"), True)
    if "max_checkpoints" in raw:
        try:
            settings["max_checkpoints"] = max(1, min(1000, int(raw.get("max_checkpoints"))))
        except (TypeError, ValueError):
            pass
    if "ignored_patterns" in raw:
        value = raw.get("ignored_patterns")
        if isinstance(value, (list, tuple)):
            settings["ignored_patterns"] = [str(item) for item in value if str(item).strip()]
        elif isinstance(value, str) and value.strip():
            settings["ignored_patterns"] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            settings["ignored_patterns"] = None
    if "clean_untracked_on_rollback" in raw:
        settings["clean_untracked_on_rollback"] = _to_bool(
            raw.get("clean_untracked_on_rollback"), True
        )
    return settings


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def build_router(
    engine_factory: Callable[..., Any],
    settings_loader: Optional[Callable[[], Dict[str, Any]]] = None,
    default_cwd: Optional[Callable[[], str]] = None,
    *,
    version: str = "1.0.2",
) -> APIRouter:
    """Build the Chronos API router.

    ``engine_factory(workspace, max_checkpoints=..., ignored_patterns=...)``
    must return a :class:`~engine.ChronosEngine`-compatible object.
    """
    router = APIRouter()
    cache_lock = threading.Lock()
    engine_cache: Dict[tuple, Any] = {}

    def settings() -> Dict[str, Any]:
        if settings_loader is None:
            return dict(_DEFAULT_SETTINGS)
        try:
            return _normalize_settings(settings_loader() or {})
        except Exception:
            logger.debug("Chronos settings loader failed", exc_info=True)
            return dict(_DEFAULT_SETTINGS)

    def _fallback_cwd() -> str:
        if default_cwd is not None:
            try:
                return str(default_cwd())
            except Exception:
                pass
        return str(Path.cwd())

    def resolve_cwd(raw: Any) -> str:
        """Validate an explicit workspace path; never accept relative paths."""
        text = str(raw or "").strip()
        if not text:
            return _fallback_cwd()
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ValueError("workspace must be an absolute path")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"workspace is not a directory: {path}")
        return str(path)

    def engine_for(workspace: str) -> Any:
        current = settings()
        key = (workspace, int(current["max_checkpoints"]), tuple(current.get("ignored_patterns") or ()))
        with cache_lock:
            engine = engine_cache.get(key)
        if engine is not None:
            return engine
        engine = engine_factory(
            workspace,
            max_checkpoints=int(current["max_checkpoints"]),
            ignored_patterns=current.get("ignored_patterns"),
        )
        with cache_lock:
            engine_cache[key] = engine
            while len(engine_cache) > 8:
                engine_cache.pop(next(iter(engine_cache)))
        return engine

    def timeline_payload(cwd: Any, limit: Any) -> Dict[str, Any]:
        try:
            workspace = resolve_cwd(cwd)
            count = _clamp_int(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
            engine = engine_for(workspace)
            checkpoints = engine.list_checkpoints(limit=count)
            current = settings()
            return {
                "status": "ok",
                "workspace": workspace,
                "checkpoints": checkpoints,
                "count": len(checkpoints),
                "config": {
                    "auto_snapshot": bool(current.get("auto_snapshot", True)),
                    "max_checkpoints": int(current.get("max_checkpoints", 50)),
                    "clean_untracked_on_rollback": bool(
                        current.get("clean_untracked_on_rollback", True)
                    ),
                },
            }
        except Exception as exc:
            logger.debug("Chronos timeline request failed", exc_info=True)
            return {"status": "error", "error": _safe_error(exc)}

    def diff_payload(
        cwd: Any,
        checkpoint_id: Any = None,
        from_id: Any = None,
        to_id: Any = None,
        max_chars: Any = None,
    ) -> Dict[str, Any]:
        try:
            workspace = resolve_cwd(cwd)
            engine = engine_for(workspace)
            limit = _clamp_int(max_chars, 400_000, MIN_DIFF_CHARS, MAX_DIFF_CHARS)
            checkpoint = str(checkpoint_id or "").strip()
            if checkpoint and not from_id and not to_id:
                result = engine.get_checkpoint_diff(checkpoint, max_chars=limit)
            else:
                result = engine.get_diff(
                    str(from_id) if from_id else None,
                    str(to_id) if to_id else "HEAD",
                    max_chars=limit,
                )
            if isinstance(result, dict):
                result.setdefault("workspace", workspace)
            return result
        except Exception as exc:
            logger.debug("Chronos diff request failed", exc_info=True)
            return {"status": "error", "error": _safe_error(exc)}

    def rollback_payload(
        cwd: Any,
        checkpoint_id: Any,
        clean: Any = None,
        create_safety: Any = True,
    ) -> Dict[str, Any]:
        try:
            workspace = resolve_cwd(cwd)
            target = str(checkpoint_id or "").strip()
            if not target:
                return {"status": "error", "error": "checkpoint_id is required"}
            engine = engine_for(workspace)
            current = settings()
            if clean is None:
                clean = bool(current.get("clean_untracked_on_rollback", True))
            result = engine.rollback(
                target,
                clean=_to_bool(clean, True),
                create_safety=_to_bool(create_safety, True),
            )
            if isinstance(result, dict):
                result.setdefault("workspace", workspace)
            return result
        except Exception as exc:
            logger.debug("Chronos rollback request failed", exc_info=True)
            return {"status": "error", "error": _safe_error(exc)}

    def health_payload(cwd: Any) -> Dict[str, Any]:
        try:
            workspace = resolve_cwd(cwd)
            engine = engine_for(workspace)
            result = engine.verify()
            if isinstance(result, dict):
                result.setdefault("version", version)
                result.setdefault("config", settings())
            return result
        except Exception as exc:
            return {"status": "error", "error": _safe_error(exc), "version": version}

    # -- routes ---------------------------------------------------------
    @router.get("/")
    async def index() -> Dict[str, Any]:
        return {
            "status": "ok",
            "plugin": "hermes-chronos",
            "version": version,
            "endpoints": ["health", "timeline", "diff", "checkpoint/{id}", "rollback"],
        }

    @router.get("/health")
    async def health(cwd: str = "") -> Dict[str, Any]:
        return await asyncio.to_thread(health_payload, cwd)

    @router.post("/health")
    async def health_post(body: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        return await asyncio.to_thread(health_payload, (body or {}).get("cwd"))

    @router.get("/timeline")
    async def timeline(cwd: str = "", limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        return await asyncio.to_thread(timeline_payload, cwd, limit)

    @router.post("/timeline")
    async def timeline_post(body: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        body = body or {}
        return await asyncio.to_thread(timeline_payload, body.get("cwd"), body.get("limit"))

    @router.get("/diff")
    async def diff(
        cwd: str = "",
        id: str = "",
        from_ref: str = Query(default="", alias="from"),
        to: str = "",
        max_chars: int = 400_000,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(diff_payload, cwd, id, from_ref, to, max_chars)

    @router.post("/diff")
    async def diff_post(body: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        body = body or {}
        return await asyncio.to_thread(
            diff_payload,
            body.get("cwd"),
            body.get("id") or body.get("checkpoint_id"),
            body.get("from"),
            body.get("to"),
            body.get("max_chars"),
        )

    @router.get("/checkpoint/{checkpoint_id}")
    async def checkpoint(checkpoint_id: str, cwd: str = "") -> Dict[str, Any]:
        try:
            workspace = resolve_cwd(cwd)
            engine = engine_for(workspace)
            metadata = engine.checkpoint_metadata(checkpoint_id)
            if metadata.get("status") != "ok":
                return metadata
            metadata["diff"] = await asyncio.to_thread(
                diff_payload, workspace, checkpoint_id, None, None, 400_000
            )
            metadata["workspace"] = workspace
            return metadata
        except Exception as exc:
            return {"status": "error", "error": _safe_error(exc)}

    @router.post("/rollback")
    async def rollback(body: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        body = body or {}
        return await asyncio.to_thread(
            rollback_payload,
            body.get("cwd"),
            body.get("checkpoint_id") or body.get("id") or body.get("target"),
            body.get("clean"),
            body.get("create_safety", True),
        )

    return router


__all__ = ["build_router"]
