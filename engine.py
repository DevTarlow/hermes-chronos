"""Hermes Chronos — isolated shadow-git checkpoint engine.

The engine keeps a private git object database under
``<workspace>/.hermes/chronos/repo`` and points it at the workspace with
``core.worktree``.  It never opens, creates, moves, or rewrites the user's
``.git`` directory: every git command is scoped with ``--git-dir`` and
``--work-tree`` and runs with a private git config, so user aliases, hooks,
signing keys and excludes cannot interfere with a checkpoint.

Design notes
------------
* **Subprocess git, not GitPython, for mutations.**  GitPython derives the
  working tree from the repository location.  With a separate git directory
  that heuristic points at the shadow database itself, not the workspace.
  Driving ``git`` with an explicit ``--work-tree`` is deterministic and
  thread-safe; GitPython is used only for optional read-only introspection.
* **Restore does not move ``HEAD``.**  ``git read-tree --reset -u <commit>``
  updates the index and working tree to the snapshot while leaving the
  checkpoint branch at its newest commit.  The next checkpoint is therefore a
  normal forward commit (a "revert" commit), and no checkpoint ever becomes
  unreachable because a rollback happened.
* **Rollback is reversible.**  A safety checkpoint of the pre-rollback state is
  taken before any file is touched.
* **Honest retention.**  Checkpoints are content-addressed git objects.  The
  engine limits how many are listed and compacts the shadow repo when history
  grows past ``max_checkpoints + 10``; Git keeps the deltas, so storage tracks
  real changes rather than the number of checkpoints.  Compaction runs in a
  background thread, never inside ``pre_tool_call``.
* **Every git call has a deadline.**  :func:`git_budget` bounds a whole
  checkpoint, so a workspace too large to snapshot in the hook's time slice is
  skipped with a log line instead of stalling the tool call.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Commit-message prefix used to embed checkpoint metadata.
CHECKPOINT_PREFIX = "CHRONOS_CP:"
#: Git's well-known empty tree object (used for the diff of a root checkpoint).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_IGNORED_PATTERNS: Tuple[str, ...] = (
    ".git",
    ".hermes",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    ".DS_Store",
)

_GIT_IDENTITY = ("Hermes Chronos", "chronos@hermes.agent")
_MAX_PATTERN_LENGTH = 200
_MAX_PATTERNS = 200
_MAX_ERROR_CHARS = 2000
_MAX_DIFF_CHARS = 400_000
_MAX_STAT_CHARS = 20_000

try:  # Optional: declared in plugin.yaml, but the engine still works without it.
    from pydantic import BaseModel as _PydanticBaseModel
except Exception:  # pragma: no cover - dependency is installed by Hermes normally
    _PydanticBaseModel = None

try:  # Optional read-only introspection only; never used for mutations.
    from git import Repo as _GitPythonRepo
except Exception:  # pragma: no cover
    _GitPythonRepo = None

try:
    import fcntl as _fcntl
except Exception:  # pragma: no cover - Windows
    _fcntl = None


class ChronosError(RuntimeError):
    """A user-facing Chronos failure (bad workspace, missing checkpoint, git error)."""


if _PydanticBaseModel is not None:

    class CheckpointMetadata(_PydanticBaseModel):  # type: ignore[misc, valid-type]
        """Validated shape of the JSON stored in a checkpoint commit message."""

        desc: str = ""
        tool: str = "manual"
        turn: Optional[int] = None
        timestamp: int = 0
        session_id: str = ""
        turn_id: str = ""
        task_id: str = ""
        version: int = 1

else:  # pragma: no cover - exercised only when pydantic is unavailable

    class CheckpointMetadata:  # type: ignore[no-redef]
        """Fallback metadata container used when pydantic is not installed."""

        __slots__ = ("desc", "tool", "turn", "timestamp", "session_id", "turn_id", "task_id", "version")

        def __init__(self, **kwargs: Any) -> None:
            self.desc = str(kwargs.get("desc") or "")
            self.tool = str(kwargs.get("tool") or "manual")
            turn = kwargs.get("turn")
            self.turn = int(turn) if isinstance(turn, (int, float)) and not isinstance(turn, bool) else None
            self.timestamp = int(kwargs.get("timestamp") or 0)
            self.session_id = str(kwargs.get("session_id") or "")
            self.turn_id = str(kwargs.get("turn_id") or "")
            self.task_id = str(kwargs.get("task_id") or "")
            self.version = int(kwargs.get("version") or 1)

        def as_dict(self) -> Dict[str, Any]:
            return {slot: getattr(self, slot) for slot in self.__slots__}


def _metadata_dict(metadata: CheckpointMetadata) -> Dict[str, Any]:
    if hasattr(metadata, "model_dump"):
        return metadata.model_dump()  # type: ignore[no-any-return]
    if hasattr(metadata, "dict"):
        return metadata.dict()  # type: ignore[no-any-return]
    return metadata.as_dict()  # type: ignore[attr-defined]


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def validate_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized metadata dict; malformed values degrade to safe defaults."""
    if not isinstance(data, dict):
        data = {}
    turn = data.get("turn")
    clean: Dict[str, Any] = {
        "desc": str(data.get("desc") or "")[:500],
        "tool": str(data.get("tool") or "manual")[:80],
        "turn": _coerce_int(turn) if isinstance(turn, (int, float)) and not isinstance(turn, bool) else None,
        "timestamp": _coerce_int(data.get("timestamp"), 0),
        "session_id": str(data.get("session_id") or "")[:200],
        "turn_id": str(data.get("turn_id") or "")[:200],
        "task_id": str(data.get("task_id") or "")[:200],
        "version": 1,
    }
    if _PydanticBaseModel is not None:
        try:
            return _metadata_dict(CheckpointMetadata(**clean))
        except Exception:
            pass
    return clean


#: Remaining wall-clock seconds a single checkpoint may spend in git.  Hermes
#: runs ``pre_tool_call`` with a 30 s callback budget and fails CLOSED when a
#: callback overruns it, so every git invocation is bounded by this deadline.
_CALL_DEADLINE: ContextVar[Optional[float]] = ContextVar("chronos_call_deadline", default=None)
#: Smallest slice worth handing to a subprocess; below this we give up instead.
_MIN_GIT_SLICE = 0.75


@contextmanager
def git_budget(seconds: Optional[float]):
    """Bound every git call made inside this block to ``seconds`` wall clock.

    Nested budgets keep the tightest deadline, so a caller that already has a
    budget (a tool) cannot be stretched by an inner one (a safety checkpoint).
    "git budget exhausted" is the error raised when the time is gone; callers
    treat it as "skip this checkpoint", never as "block the tool".
    """
    try:
        requested = float(seconds) if seconds else 0.0
    except (TypeError, ValueError):
        requested = 0.0
    if requested <= 0:
        yield
        return
    outer = _CALL_DEADLINE.get()
    deadline = time.monotonic() + requested
    if outer is not None:
        deadline = min(deadline, outer)
    token = _CALL_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _CALL_DEADLINE.reset(token)


def _remaining_budget() -> Optional[float]:
    """Seconds left in the active budget, or ``None`` when no budget is set."""
    deadline = _CALL_DEADLINE.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def resolve_workspace(explicit: Optional[str] = None) -> str:
    """Resolve the workspace for a tool, hook, command, or API call.

    Uses Hermes' documented session working-directory helper when it is
    available, and falls back to the process directory for standalone use.
    """
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:
        return os.getcwd()


_LOCK_REGISTRY: Dict[str, threading.RLock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def _thread_lock_for(path: str) -> threading.RLock:
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(path)
        if lock is None:
            lock = threading.RLock()
            _LOCK_REGISTRY[path] = lock
        return lock


_ENGINE_CACHE: "Dict[Tuple[str, int, Tuple[str, ...]], ChronosEngine]" = {}
_ENGINE_CACHE_ORDER: List[Tuple[str, int, Tuple[str, ...]]] = []
_ENGINE_CACHE_GUARD = threading.Lock()


def get_engine(
    workspace: Optional[str] = None,
    max_checkpoints: int = 50,
    ignored_patterns: Optional[Sequence[str]] = None,
) -> "ChronosEngine":
    """Return a cached engine for *workspace* (bounded LRU cache)."""
    root = resolve_workspace(workspace)
    patterns = tuple(ignored_patterns or DEFAULT_IGNORED_PATTERNS)
    key = (root, int(max_checkpoints), patterns)
    with _ENGINE_CACHE_GUARD:
        engine = _ENGINE_CACHE.get(key)
        if engine is not None:
            _ENGINE_CACHE_ORDER.remove(key)
            _ENGINE_CACHE_ORDER.append(key)
            return engine
    engine = ChronosEngine(root, max_checkpoints=max_checkpoints, ignored_patterns=list(patterns))
    with _ENGINE_CACHE_GUARD:
        _ENGINE_CACHE[key] = engine
        _ENGINE_CACHE_ORDER.append(key)
        while len(_ENGINE_CACHE_ORDER) > 16:
            stale = _ENGINE_CACHE_ORDER.pop(0)
            _ENGINE_CACHE.pop(stale, None)
    return engine


def clear_engine_cache() -> None:
    """Drop cached engines (tests and profile switches)."""
    with _ENGINE_CACHE_GUARD:
        _ENGINE_CACHE.clear()
        _ENGINE_CACHE_ORDER.clear()


class ChronosEngine:
    """Manage an isolated shadow-git checkpoint database for one workspace."""

    def __init__(
        self,
        workspace_root: str,
        max_checkpoints: int = 50,
        ignored_patterns: Optional[List[str]] = None,
        *,
        git_executable: str = "git",
        timeout: float = 120.0,
    ) -> None:
        if not workspace_root:
            raise ChronosError("Chronos needs a workspace directory.")
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ChronosError(f"Workspace is not a directory: {root}")
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise ChronosError(f"Refusing to checkpoint a broad system directory: {root}")

        try:
            max_checkpoints = int(max_checkpoints)
        except (TypeError, ValueError):
            max_checkpoints = 50
        self.max_checkpoints = max(1, min(1000, max_checkpoints))
        self.timeout = max(5.0, float(timeout or 120.0))

        self.workspace_root = str(root)
        self.git_executable = git_executable or "git"
        self.chronos_dir = os.path.join(self.workspace_root, ".hermes", "chronos")
        self.git_dir = os.path.join(self.chronos_dir, "repo")
        self.config_file = os.path.join(self.chronos_dir, "gitconfig")
        self.exclude_file = os.path.join(self.chronos_dir, "exclude")
        #: Keeps the shadow database invisible to the *user's* git repo.
        self.shadow_ignore_file = os.path.join(self.chronos_dir, ".gitignore")
        self.lock_file = os.path.join(self.chronos_dir, "lock")
        self.hooks_dir = os.path.join(self.chronos_dir, "hooks")
        self.template_dir = os.path.join(self.chronos_dir, "template")
        self.ignored_patterns = self._normalize_patterns(ignored_patterns)
        self._lock = _thread_lock_for(self.workspace_root)
        self._env = self._build_env()
        self._compact_guard = threading.Lock()
        self._compacting = False

        if not shutil.which(self.git_executable):
            raise ChronosError(
                "git was not found on PATH. Install git to use Hermes Chronos."
            )

        os.makedirs(self.hooks_dir, exist_ok=True)
        os.makedirs(self.template_dir, exist_ok=True)
        with self._exclusive():
            self._ensure_repo_locked()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def repo_path(self) -> str:
        return self.git_dir

    def create_checkpoint(
        self,
        description: str,
        tool_name: Optional[str] = None,
        turn_index: Optional[int] = None,
        *,
        session_id: str = "",
        turn_id: str = "",
        task_id: str = "",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Snapshot the workspace and commit it to the shadow database.

        ``timeout`` bounds the *whole* snapshot, not just one git call.  Hooks
        pass a short budget so an unexpectedly huge workspace degrades to a
        logged skip instead of overrunning Hermes' 30 s callback budget.
        """
        try:
            with git_budget(timeout):
                with self._exclusive():
                    return self._create_checkpoint_locked(
                        description,
                        tool_name=tool_name,
                        turn_index=turn_index,
                        session_id=session_id,
                        turn_id=turn_id,
                        task_id=task_id,
                        timeout=timeout,
                    )
        except ChronosError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # defensive: hooks/tools must never see a raise
            logger.exception("Chronos checkpoint failed")
            return {"status": "error", "error": self._safe_error(exc)}

    def rollback(
        self,
        checkpoint_id: str,
        *,
        clean: bool = True,
        create_safety: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Restore the workspace to *checkpoint_id*.

        A safety checkpoint of the current state is committed first, then the
        index and working tree are reset to the target tree.  With ``clean``
        (the default) untracked, non-ignored files are removed so the workspace
        matches the snapshot exactly.  With ``clean=False`` those files are
        restored afterward, so they survive the rollback as untracked files.
        """
        try:
            with git_budget(timeout):
                with self._exclusive():
                    return self._rollback_locked(
                        checkpoint_id,
                        clean=clean,
                        create_safety=create_safety,
                        timeout=timeout,
                    )
        except ChronosError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            logger.exception("Chronos rollback failed")
            return {"status": "error", "error": self._safe_error(exc)}

    def _rollback_locked(
        self,
        checkpoint_id: str,
        *,
        clean: bool = True,
        create_safety: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not checkpoint_id or not str(checkpoint_id).strip():
            return {"status": "error", "error": "A checkpoint id is required."}
        target = self._resolve_checkpoint_locked(str(checkpoint_id).strip())

        # Capture the untracked set BEFORE the safety checkpoint: the safety
        # commit stages those files, so after it they are no longer reported as
        # untracked.
        untracked_before = self._untracked_locked()

        safety_id: Optional[str] = None
        safety_hash: Optional[str] = None
        if create_safety:
            safety = self._create_checkpoint_locked(
                "Pre-rollback safety snapshot",
                tool_name="chronos_rollback",
                turn_index=None,
                timeout=timeout,
            )
            if safety.get("status") != "ok":
                return {
                    "status": "error",
                    "error": "Aborting rollback: could not create a safety checkpoint "
                    f"({safety.get('error', 'unknown error')}).",
                }
            safety_id = safety.get("checkpoint_id")
            safety_hash = safety.get("commit_hash")

        head = self._head_locked()
        changes_undone = self._changed_paths_between_locked(target, head) if head else []
        untracked_removed = 0
        if clean:
            untracked_removed = len(untracked_before)
            self._git("clean", "-fd", "--", ".", check=False, timeout=timeout)
        self._git("read-tree", "--reset", "-u", target, timeout=timeout)
        self._git("update-index", "-q", "--refresh", check=False, timeout=timeout)

        untracked_preserved = 0
        if not clean and create_safety and safety_hash and untracked_before:
            untracked_preserved = self._restore_untracked_from_commit(
                safety_hash, untracked_before, target
            )

        summary = self._commit_summary_locked(target)
        restored = len(self._tracked_paths_locked(target))
        return {
            "status": "ok",
            "message": f"Workspace restored to checkpoint {target[:8]}.",
            "restored_checkpoint": target[:8],
            "restored_hash": target,
            "commit_summary": summary,
            "safety_checkpoint": safety_id,
            "clean": bool(clean),
            "files_changed": len(changes_undone),
            "restored_files": restored,
            "untracked_removed": untracked_removed if clean else 0,
            "untracked_preserved": untracked_preserved,
        }

    def get_diff(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = "HEAD",
        *,
        max_chars: int = _MAX_DIFF_CHARS,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return a unified diff.

        * ``from_id`` and ``to_id`` both set: diff between two checkpoints.
        * only ``from_id``: diff from that checkpoint to the current HEAD.
        * neither, workspace dirty: current uncommitted changes against HEAD.
        * neither, workspace clean: the most recent checkpoint's diff
          (``HEAD~1..HEAD``), or the full tree for the root checkpoint.
        * ``to_id="WORKTREE"``: diff from ``from_id`` to the working tree.
        """
        try:
            with self._exclusive():
                head = self._head_locked()
                if not head and not from_id:
                    return {
                        "status": "ok",
                        "diff": "(No checkpoints recorded yet)",
                        "stat": "",
                        "from": None,
                        "to": None,
                        "untracked": [],
                        "truncated": False,
                        "scope": "empty",
                    }
                scope = "checkpoints"
                untracked: List[str] = []
                if from_id:
                    frm = EMPTY_TREE if str(from_id).strip() == EMPTY_TREE else self._resolve_checkpoint_locked(str(from_id))
                    if str(to_id or "").strip().upper() == "WORKTREE":
                        diff_args = ["diff", "--no-color", "--no-ext-diff", "--unified=3", frm, "--", "."]
                        base_label = frm
                        target_label = "WORKTREE"
                        scope = "worktree"
                    else:
                        target = self._resolve_checkpoint_locked(str(to_id or "HEAD"))
                        diff_args = ["diff", "--no-color", "--no-ext-diff", "--unified=3", frm, target, "--"]
                        base_label = frm
                        target_label = target
                else:
                    status = self._git(
                        "status", "--porcelain", "--untracked-files=normal", check=False, timeout=timeout
                    )
                    dirty = bool((status.stdout or "").strip())
                    if dirty:
                        diff_args = ["diff", "--no-color", "--no-ext-diff", "--unified=3", "HEAD", "--"]
                        base_label = head
                        target_label = "WORKTREE"
                        scope = "worktree"
                        untracked = self._untracked_locked()[:200]
                    else:
                        parent = self._git(
                            "rev-parse", "--verify", "--quiet", f"{head}^", check=False, timeout=timeout
                        )
                        base = (
                            parent.stdout.strip().splitlines()[-1]
                            if parent.returncode == 0 and (parent.stdout or "").strip()
                            else EMPTY_TREE
                        )
                        diff_args = [
                            "diff", "--no-color", "--no-ext-diff", "--unified=3", base, head, "--",
                        ]
                        base_label = base
                        target_label = head
                        scope = "last_checkpoint" if base != EMPTY_TREE else "root_checkpoint"

                result = self._git(*diff_args, check=False, timeout=timeout)
                text = result.stdout or ""
                truncated = len(text) > max_chars
                if truncated:
                    text = text[:max_chars].rsplit("\n", 1)[0] + "\n… [diff truncated]"

                stat_args = list(diff_args)
                stat_args[1:1] = ["--stat"]
                stat = self._git(*stat_args, check=False, timeout=timeout).stdout or ""
                if len(stat) > _MAX_STAT_CHARS:
                    stat = stat[:_MAX_STAT_CHARS] + "\n… [stat truncated]"

                if result.returncode != 0 and not text:
                    return {
                        "status": "error",
                        "error": (result.stderr or "git diff failed").strip()[:_MAX_ERROR_CHARS],
                    }
                return {
                    "status": "ok",
                    "diff": text or "(No modifications)",
                    "stat": stat,
                    "from": base_label,
                    "to": target_label,
                    "untracked": untracked,
                    "truncated": truncated,
                    "scope": scope,
                }
        except ChronosError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            logger.exception("Chronos diff failed")
            return {"status": "error", "error": self._safe_error(exc)}

    def get_checkpoint_diff(self, checkpoint_id: str, *, max_chars: int = _MAX_DIFF_CHARS) -> Dict[str, Any]:
        """Diff the changes introduced by one checkpoint against its parent."""
        try:
            with self._exclusive():
                target = self._resolve_checkpoint_locked(str(checkpoint_id))
                parent = self._git("rev-parse", "--verify", "--quiet", f"{target}^", check=False)
                base = parent.stdout.strip() if parent.returncode == 0 and parent.stdout.strip() else EMPTY_TREE
            result = self.get_diff(base, target, max_chars=max_chars)
            if result.get("status") == "ok":
                result["checkpoint_id"] = target[:8]
                result["checkpoint_hash"] = target
            return result
        except ChronosError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            return {"status": "error", "error": self._safe_error(exc)}

    def list_checkpoints(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent checkpoints, newest first."""
        try:
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            limit = 20
        try:
            with self._exclusive():
                return self._list_checkpoints_locked(limit)
        except Exception:
            logger.exception("Chronos list_checkpoints failed")
            return []

    def checkpoint_metadata(self, checkpoint_id: str) -> Dict[str, Any]:
        """Return metadata for one checkpoint."""
        try:
            with self._exclusive():
                target = self._resolve_checkpoint_locked(str(checkpoint_id))
                metadata = self._metadata_for_locked(target)
                return {
                    "status": "ok",
                    "id": target[:8],
                    "hash": target,
                    "description": metadata.get("desc", ""),
                    "tool": metadata.get("tool", "manual"),
                    "turn": metadata.get("turn"),
                    "timestamp": metadata.get("timestamp"),
                    "time_iso": self._iso(metadata.get("timestamp")),
                }
        except ChronosError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            return {"status": "error", "error": self._safe_error(exc)}

    def verify(self) -> Dict[str, Any]:
        """Return repository health and statistics (never raises)."""
        info: Dict[str, Any] = {
            "status": "ok",
            "workspace": self.workspace_root,
            "repo": self.git_dir,
            "available": False,
            "checkpoint_count": 0,
        }
        try:
            with self._exclusive():
                self._ensure_repo_locked()
                info["available"] = True
                info["head"] = self._head_locked()
                info["branch"] = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or "chronos"
                info["checkpoint_count"] = self._commit_count_locked()
                info["git_version"] = self._git("--version", check=False).stdout.strip()
                info["ignored_patterns"] = list(self.ignored_patterns)
                info["size_bytes"] = self._directory_size(self.chronos_dir)
                if _GitPythonRepo is not None:
                    try:
                        repo = _GitPythonRepo(self.git_dir)
                        info["gitpython"] = {
                            "bare": bool(repo.bare),
                            "git_dir": str(repo.git_dir),
                        }
                    except Exception as exc:  # optional introspection only
                        info["gitpython"] = {"error": str(exc)[:_MAX_ERROR_CHARS]}
                return info
        except Exception as exc:
            info["status"] = "error"
            info["error"] = self._safe_error(exc)
            return info

    # ------------------------------------------------------------------
    # Checkpoint creation / restore internals
    # ------------------------------------------------------------------
    def _create_checkpoint_locked(
        self,
        description: str,
        *,
        tool_name: Optional[str] = None,
        turn_index: Optional[int] = None,
        session_id: str = "",
        turn_id: str = "",
        task_id: str = "",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._ensure_repo_locked()
        self._git("add", "-A", "--", ".", timeout=timeout)
        head = self._head_locked()

        if head:
            no_changes = self._git("diff", "--cached", "--quiet", "HEAD", "--", check=False, timeout=timeout)
            if no_changes.returncode == 0:
                metadata = self._metadata_for_locked(head)
                return {
                    "status": "ok",
                    "created": False,
                    "checkpoint_id": head[:8],
                    "commit_hash": head,
                    "description": metadata.get("desc", description),
                    "tool": metadata.get("tool", tool_name or "manual"),
                    "turn": metadata.get("turn"),
                    "timestamp": metadata.get("timestamp", int(time.time())),
                    "message": "No filesystem changes detected since last checkpoint.",
                }
            changed_files = len(self._changed_paths_between_locked(head, None, cached=True))
        else:
            staged = self._git("ls-files", check=False)
            changed_files = len([line for line in (staged.stdout or "").splitlines() if line.strip()])

        timestamp = int(time.time())
        metadata = validate_metadata(
            {
                "desc": description or "Checkpoint",
                "tool": tool_name or "manual",
                "turn": turn_index,
                "timestamp": timestamp,
                "session_id": session_id,
                "turn_id": turn_id,
                "task_id": task_id,
            }
        )
        message = self._commit_message(metadata)
        result = self._git(
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "--allow-empty",
            "-m",
            message,
            check=False,
            timeout=timeout,
        )
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}".strip()
            if "nothing to commit" in combined.lower():
                head = self._head_locked()
                metadata = self._metadata_for_locked(head)
                return {
                    "status": "ok",
                    "created": False,
                    "checkpoint_id": head[:8],
                    "commit_hash": head,
                    "description": metadata.get("desc", description),
                    "tool": metadata.get("tool", tool_name or "manual"),
                    "turn": metadata.get("turn"),
                    "timestamp": metadata.get("timestamp", timestamp),
                    "message": "No filesystem changes detected since last checkpoint.",
                }
            raise ChronosError(f"git commit failed: {combined[:_MAX_ERROR_CHARS]}")

        commit = self._head_locked()
        if not commit:
            raise ChronosError("Checkpoint commit succeeded but HEAD is invalid.")
        # Compaction (reflog expire + git gc) is far too slow to run inside a
        # tool-call hook: Hermes blocks the tool after 30 s.  Decide here, do it
        # in the background.
        if self._compaction_due_locked():
            self._schedule_compaction()
        return {
            "status": "ok",
            "created": True,
            "checkpoint_id": commit[:8],
            "commit_hash": commit,
            "description": metadata.get("desc", description),
            "tool": metadata.get("tool", tool_name or "manual"),
            "turn": metadata.get("turn"),
            "timestamp": timestamp,
            "files_changed": changed_files,
            "message": f"Checkpoint {commit[:8]} created.",
        }

    # ------------------------------------------------------------------
    # Git plumbing
    # ------------------------------------------------------------------
    def _ensure_repo_locked(self) -> None:
        if not os.path.isdir(self.git_dir):
            os.makedirs(self.chronos_dir, exist_ok=True)
            init = self._run_raw(
                [
                    self.git_executable,
                    "init",
                    "--quiet",
                    "--bare",
                    "--initial-branch=chronos",
                    f"--template={self.template_dir}",
                    self.git_dir,
                ],
                check=False,
            )
            if init.returncode != 0:
                raise ChronosError(
                    "Could not initialise the Chronos shadow repository: "
                    f"{(init.stderr or init.stdout).strip()[:_MAX_ERROR_CHARS]}"
                )
        else:
            probe = self._git("rev-parse", "--git-dir", check=False)
            if probe.returncode != 0:
                raise ChronosError(
                    f"{self.git_dir} exists but is not a usable git repository: "
                    f"{(probe.stderr or '').strip()[:_MAX_ERROR_CHARS]}"
                )

        self._write_git_config()
        self._write_excludes()
        self._write_shadow_ignore()

        if not self._head_locked():
            self._git("add", "-A", "--", ".", check=False)
            baseline = validate_metadata(
                {
                    "desc": "Root baseline snapshot",
                    "tool": "system",
                    "turn": 0,
                    "timestamp": int(time.time()),
                }
            )
            self._git(
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "--allow-empty",
                "-m",
                self._commit_message(baseline),
                check=False,
            )
            if not self._head_locked():
                raise ChronosError("Could not create the Chronos baseline commit.")

    def _git(
        self,
        *args: str,
        check: bool = True,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        command = [
            self.git_executable,
            "-c",
            "safe.directory=*",
            "-c",
            f"core.excludesfile={self.exclude_file}",
            "-c",
            f"core.hooksPath={self.hooks_dir}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "--git-dir",
            self.git_dir,
            "--work-tree",
            self.workspace_root,
            *args,
        ]
        return self._run_raw(command, check=check, timeout=timeout)

    def _run_raw(
        self,
        command: Sequence[str],
        *,
        check: bool = True,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        effective = self.timeout if timeout is None else min(self.timeout, float(timeout))
        remaining = _remaining_budget()
        if remaining is not None:
            if remaining < _MIN_GIT_SLICE:
                raise ChronosError(
                    "git budget exhausted; Chronos skipped this operation instead of "
                    "overrunning the tool-call hook budget"
                )
            effective = min(effective, remaining)
        try:
            result = subprocess.run(
                list(command),
                cwd=self.workspace_root,
                env=self._env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._drop_stale_index_lock()
            raise ChronosError(f"git timed out after {effective:.1f}s") from exc
        except OSError as exc:
            raise ChronosError(f"Could not run git: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ChronosError(f"git {' '.join(command[1:])} failed: {detail[:_MAX_ERROR_CHARS]}")
        return result

    def _drop_stale_index_lock(self) -> None:
        """Clear a lock left behind by a killed ``git`` (we hold ``_exclusive``)."""
        for name in ("index.lock", "HEAD.lock"):
            lock = os.path.join(self.git_dir, name)
            try:
                os.remove(lock)
            except FileNotFoundError:
                continue
            except OSError:
                logger.debug("Chronos could not remove stale %s", lock, exc_info=True)

    def _config_set(self, key: str, value: str) -> None:
        result = self._run_raw(
            [self.git_executable, "config", "--file", self.config_file, key, value],
            check=False,
        )
        if result.returncode != 0:
            raise ChronosError(
                f"Could not write Chronos git config {key}: "
                f"{(result.stderr or '').strip()[:_MAX_ERROR_CHARS]}"
            )

    def _write_git_config(self) -> None:
        os.makedirs(self.chronos_dir, exist_ok=True)
        values = (
            ("core.worktree", self.workspace_root),
            ("core.bare", "false"),
            ("core.excludesFile", self.exclude_file),
            ("core.hooksPath", self.hooks_dir),
            ("core.fsmonitor", "false"),
            ("user.name", _GIT_IDENTITY[0]),
            ("user.email", _GIT_IDENTITY[1]),
            ("commit.gpgsign", "false"),
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("advice.detachedHead", "false"),
            ("safe.directory", "*"),
        )
        for key, value in values:
            self._config_set(key, value)

    def _write_excludes(self) -> None:
        lines: List[str] = []
        seen = set()
        for pattern in self.ignored_patterns:
            pattern = pattern.strip()
            if not pattern or pattern in seen:
                continue
            seen.add(pattern)
            lines.append(pattern)
            if not pattern.endswith("/**") and not pattern.endswith("/"):
                lines.append(pattern.rstrip("/") + "/**")
        payload = "\n".join(lines) + "\n"
        self._atomic_write(self.exclude_file, payload)

    def _write_shadow_ignore(self) -> None:
        """Hide ``.hermes/chronos/`` from the *user's* repository.

        Chronos' own ``core.excludesFile`` keeps the database out of snapshots,
        but nothing stops the user's git from reporting the snapshot database as
        untracked.  A ``.gitignore`` containing ``*`` inside ``.hermes/chronos/``
        (the standard trick from ``git init --bare``) makes the whole directory
        invisible, without touching the user's ``.gitignore`` or their
        ``.hermes/`` siblings.  It is never snapshotted: ``.hermes`` is excluded.
        """
        payload = (
            "# Written by Hermes Chronos. Keeps the shadow repository out of\n"
            "# the workspace's own git status. Safe to delete when Chronos is\n"
            "# uninstalled.\n"
            "*\n"
        )
        try:
            existing = ""
            if os.path.exists(self.shadow_ignore_file):
                with open(self.shadow_ignore_file, "r", encoding="utf-8") as handle:
                    existing = handle.read()
            if existing == payload:
                return
            self._atomic_write(self.shadow_ignore_file, payload)
        except OSError:
            logger.debug("Chronos could not write %s", self.shadow_ignore_file, exc_info=True)

    def _normalize_patterns(self, patterns: Optional[Iterable[str]]) -> Tuple[str, ...]:
        raw = list(patterns) if patterns else list(DEFAULT_IGNORED_PATTERNS)
        cleaned: List[str] = []
        for item in raw[:_MAX_PATTERNS]:
            if not isinstance(item, str):
                continue
            pattern = item.strip()
            if not pattern or len(pattern) > _MAX_PATTERN_LENGTH:
                continue
            if any(ch in pattern for ch in ("\x00", "\n", "\r")):
                continue
            if pattern.startswith("!"):
                # A negated user pattern could re-include the shadow database or .git.
                continue
            cleaned.append(pattern)
        for required in (".hermes", ".git"):
            if required not in cleaned:
                cleaned.append(required)
        # Order matters for readability only; excludes are commutative.
        return tuple(dict.fromkeys(cleaned))

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("GIT_"):
                env.pop(key, None)
        env.update(
            {
                "GIT_CONFIG_GLOBAL": self.config_file,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "LC_ALL": "C",
                "LANG": "C",
                "GIT_AUTHOR_NAME": _GIT_IDENTITY[0],
                "GIT_AUTHOR_EMAIL": _GIT_IDENTITY[1],
                "GIT_COMMITTER_NAME": _GIT_IDENTITY[0],
                "GIT_COMMITTER_EMAIL": _GIT_IDENTITY[1],
            }
        )
        return env

    # ------------------------------------------------------------------
    # History / refs
    # ------------------------------------------------------------------
    def _head_locked(self) -> str:
        result = self._git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _commit_count_locked(self) -> int:
        result = self._git("rev-list", "--count", "HEAD", check=False)
        try:
            return int((result.stdout or "0").strip())
        except ValueError:
            return 0

    def _resolve_checkpoint_locked(self, ref: str) -> str:
        ref = (ref or "").strip()
        if not ref or ref.lower() in {"head", "latest", "last", "tip"}:
            head = self._head_locked()
            if not head:
                raise ChronosError("No checkpoints recorded yet.")
            return head
        if ref == EMPTY_TREE:
            return EMPTY_TREE
        if ref.startswith("-") or any(ch in ref for ch in ("\x00", "\n", "\r", "\t", " ")):
            raise ChronosError(f"Invalid checkpoint reference: {ref!r}")

        # Try the ref as a commit first.  An 8-character checkpoint id can be all
        # digits (e.g. "31169324"); treating it as an index would address the
        # 31-millionth commit instead of the id the user copied.
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if result.returncode == 0 and (result.stdout or "").strip():
            sha = (result.stdout or "").strip().splitlines()[-1]
            head = self._head_locked()
            if head and sha != head:
                ancestor = self._git("merge-base", "--is-ancestor", sha, head, check=False)
                if ancestor.returncode != 0:
                    raise ChronosError(
                        f"Commit {ref!r} is not a checkpoint in this workspace's Chronos history."
                    )
            return sha

        if ref.isdigit():
            index = int(ref)
            if index < 1:
                raise ChronosError("Checkpoint index must be 1 or greater.")
            result = self._git("rev-list", "--max-count", str(index), "HEAD", check=False)
            rows = [line for line in (result.stdout or "").splitlines() if line.strip()]
            if len(rows) < index:
                raise ChronosError(f"Checkpoint index {index} is out of range.")
            return rows[-1]

        raise ChronosError(f"Checkpoint {ref!r} was not found.")

    def _list_checkpoints_locked(self, limit: int) -> List[Dict[str, Any]]:
        if not self._head_locked():
            return []
        result = self._git(
            "log",
            f"--max-count={limit}",
            "--format=%H%x1f%ct%x1f%s%x1e",
            "HEAD",
            check=False,
        )
        if result.returncode != 0:
            return []
        rows: List[Dict[str, Any]] = []
        for record in (result.stdout or "").split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            parts = record.split("\x1f", 2)
            if len(parts) < 3:
                continue
            commit_hash, raw_ts, summary = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not re.fullmatch(r"[0-9a-f]{40}", commit_hash):
                continue
            try:
                timestamp = int(raw_ts)
            except ValueError:
                timestamp = 0
            metadata = self._parse_commit_summary(summary)
            rows.append(
                {
                    "id": commit_hash[:8],
                    "short_id": commit_hash[:8],
                    "hash": commit_hash,
                    "description": metadata.get("desc", summary),
                    "tool": metadata.get("tool", "manual"),
                    "turn": metadata.get("turn"),
                    "time": timestamp,
                    "timestamp": timestamp,
                    "time_iso": self._iso(timestamp),
                    "is_baseline": metadata.get("tool") == "system" and metadata.get("turn") == 0,
                    "subject": summary,
                }
            )
        return rows

    def _metadata_for_locked(self, commit_hash: str) -> Dict[str, Any]:
        result = self._git("show", "-s", "--format=%s", commit_hash, check=False)
        summary = (result.stdout or "").strip()
        return self._parse_commit_summary(summary)

    def _parse_commit_summary(self, summary: str) -> Dict[str, Any]:
        summary = (summary or "").strip()
        if not summary.startswith(CHECKPOINT_PREFIX):
            return {"desc": summary or "Checkpoint", "tool": "manual", "turn": None, "timestamp": 0}
        raw = summary[len(CHECKPOINT_PREFIX) :].strip()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {"desc": summary, "tool": "manual", "turn": None, "timestamp": 0}
        if not isinstance(data, dict):
            return {"desc": summary, "tool": "manual", "turn": None, "timestamp": 0}
        return validate_metadata(data)

    def _commit_message(self, metadata: Dict[str, Any]) -> str:
        payload = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return f"{CHECKPOINT_PREFIX} {payload}"

    def _commit_summary_locked(self, commit_hash: str) -> str:
        result = self._git("show", "-s", "--format=%s", commit_hash, check=False)
        return (result.stdout or "").strip() or commit_hash[:8]

    def _changed_paths_between_locked(
        self,
        older: str,
        newer: Optional[str] = None,
        *,
        cached: bool = False,
    ) -> List[str]:
        args = ["diff", "--name-only"]
        if cached:
            args.append("--cached")
        args.append(older)
        if newer:
            args.append(newer)
        args.extend(["--", "."])
        result = self._git(*args, check=False)
        return [line for line in (result.stdout or "").splitlines() if line.strip()]

    def _tracked_paths_locked(self, ref: str) -> List[str]:
        result = self._git("ls-tree", "-r", "--name-only", ref, check=False)
        return [line for line in (result.stdout or "").splitlines() if line.strip()]

    def _untracked_locked(self) -> List[str]:
        result = self._git("ls-files", "--others", "--exclude-standard", check=False)
        return [line for line in (result.stdout or "").splitlines() if line.strip()]

    def _restore_untracked_from_commit(self, commit: str, paths: List[str], target: str) -> int:
        """Restore pre-rollback untracked files from the safety commit.

        The safety checkpoint stages untracked files, so ``read-tree`` would
        otherwise delete them as tracked-but-absent.  Write each path back from
        the safety commit, then unstage it so it is untracked again.  Paths the
        target commit tracks are left to the target (the target wins).
        """
        candidates: List[str] = []
        for path in paths:
            tracked = self._git("ls-tree", "-r", "--name-only", target, "--", path, check=False)
            if (tracked.stdout or "").strip():
                continue
            candidates.append(path)
        if not candidates:
            return 0

        for start in range(0, len(candidates), 100):
            batch = candidates[start : start + 100]
            written = self._git("checkout", commit, "--", *batch, check=False)
            if written.returncode != 0:
                for path in batch:
                    self._git("checkout", commit, "--", path, check=False)
            # Keep the files in the working tree but out of the index again.
            self._git("rm", "--cached", "--ignore-unmatch", "--", *batch, check=False)

        preserved = 0
        for path in candidates:
            absolute = Path(self.workspace_root) / path
            if absolute.is_symlink() or absolute.exists():
                preserved += 1
        return preserved

    def _compaction_due_locked(self) -> bool:
        """True when the shadow repo has outgrown ``max_checkpoints``.

        Cheap on purpose: this runs inside ``pre_tool_call``, so it may only
        count commits.  The expensive part lives in :meth:`compact`.
        """
        if self.max_checkpoints <= 0:
            return False
        return self._commit_count_locked() > self.max_checkpoints + 10

    def _schedule_compaction(self) -> bool:
        """Start one background compaction; never blocks and never raises."""
        with self._compact_guard:
            if self._compacting:
                return False
            self._compacting = True
        thread = threading.Thread(
            target=self._compaction_worker,
            name=f"chronos-compact:{os.path.basename(self.workspace_root) or 'workspace'}",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:  # interpreter shutting down
            with self._compact_guard:
                self._compacting = False
            return False
        return True

    def _compaction_worker(self) -> None:
        try:
            self.compact()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Chronos background compaction failed", exc_info=True)
        finally:
            with self._compact_guard:
                self._compacting = False

    def compact(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Expire reflogs and repack the shadow repo.  Call out of the hot path.

        Runs in the background when history outgrows ``max_checkpoints``, and is
        also exposed for an explicit "compact now" request (dashboard health
        check, ``hermes-chronos`` maintenance).  Safe to call concurrently: the
        per-workspace lock serialises it against checkpoints and rollbacks.
        """
        try:
            with self._exclusive():
                budget = float(timeout) if timeout else max(self.timeout, 120.0)
                expire = self._git(
                    "reflog", "expire", "--expire=now", "--all",
                    check=False, timeout=min(budget, 120.0),
                )
                gc = self._git(
                    "gc", "--prune=now", "--quiet",
                    check=False, timeout=min(budget, 300.0),
                )
                ok = expire.returncode == 0 and gc.returncode == 0
                if not ok:
                    logger.info(
                        "Chronos compaction finished with errors (reflog=%s, gc=%s)",
                        expire.returncode,
                        gc.returncode,
                    )
                return {
                    "status": "ok" if ok else "error",
                    "reflog_exit": expire.returncode,
                    "gc_exit": gc.returncode,
                    "commits": self._commit_count_locked(),
                }
        except ChronosError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Chronos compaction failed", exc_info=True)
            return {"status": "error", "error": self._safe_error(exc)}

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    @contextmanager
    def _exclusive(self):
        """Serialise checkpoints for this workspace across threads and processes."""
        with self._lock:
            fd: Optional[int] = None
            try:
                if _fcntl is not None:
                    os.makedirs(self.chronos_dir, exist_ok=True)
                    fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
                    _fcntl.flock(fd, _fcntl.LOCK_EX)
                yield
            finally:
                if fd is not None:
                    try:
                        _fcntl.flock(fd, _fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    @staticmethod
    def _atomic_write(path: str, payload: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)

    @staticmethod
    def _directory_size(path: str) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        continue
        except OSError:
            return 0
        return total

    @staticmethod
    def _iso(timestamp: Any) -> str:
        try:
            value = int(timestamp)
        except (TypeError, ValueError):
            return ""
        if value <= 0:
            return ""
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        return str(exc).strip()[:_MAX_ERROR_CHARS] or exc.__class__.__name__


__all__ = [
    "CHECKPOINT_PREFIX",
    "DEFAULT_IGNORED_PATTERNS",
    "EMPTY_TREE",
    "CheckpointMetadata",
    "ChronosEngine",
    "ChronosError",
    "clear_engine_cache",
    "get_engine",
    "resolve_workspace",
    "validate_metadata",
]
