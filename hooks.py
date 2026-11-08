"""Lifecycle hooks for Hermes Chronos.

``pre_tool_call`` takes a checkpoint before a workspace-modifying tool runs and
puts ``chronos_rollback`` behind Hermes' human-approval gate.
``post_tool_call`` classifies the result.  ``transform_tool_result`` adds a
short, actionable recovery hint to a failed result so the model can inspect or
undo the change.

Hermes treats a raising ``pre_tool_call`` callback as a hard block, and kills a
callback that overruns ``plugins.hook_callback_timeout`` (30 s by default),
which also blocks the tool.  So every path here is defensive and
time-bounded: failures and slowness are logged and the tool is allowed to run.
"""

import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from .engine import git_budget
from .tools import ChronosTools, _as_bool, _config, turn_index_for

logger = logging.getLogger(__name__)

#: The one tool this plugin registers that can destroy user data.
ROLLBACK_TOOL = "chronos_rollback"

#: Wall-clock budget for a whole auto-checkpoint.  Hermes' ``pre_tool_call``
#: budget is 30 s and an overrun fails CLOSED, so stay far below it.
HOOK_BUDGET_SECONDS = 8.0
#: Consecutive budget overruns before auto-snapshots back off for a workspace.
_SLOW_FAILURE_LIMIT = 2
#: How long a workspace stays in slow-mode before one more snapshot is tried.
_SLOW_BACKOFF_SECONDS = 300.0
#: workspace -> monotonic deadline until which auto-snapshots are skipped.
_SLOW_WORKSPACES: Dict[str, float] = {}
#: workspace -> count of consecutive snapshot attempts that missed the budget.
_SLOW_STRIKES: Dict[str, int] = {}

#: Built-in tools that can change the workspace.  ``patch`` is Hermes' real
#: name; ``patch_file`` and ``file_edit`` are accepted aliases for older builds
#: and third-party tools.
#:
#: ``skill_manage`` is deliberately absent.  It writes to
#: ``get_hermes_home()/skills``, which is outside the workspace, so a checkpoint
#: taken before it contains nothing about the change and restoring it cannot
#: undo one.  Offering a rollback there was a false promise (see
#: "Rollback is filesystem-only" in the README).
MODIFYING_TOOLS = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "patch_file",
        "file_edit",
        "apply_patch",
        "delete_file",
    }
)

#: Workspace-modifying tools that can also change state Chronos cannot see or
#: restore: databases, services, remote APIs, the user's own git remotes, and
#: files outside the workspace.  Their runs are recorded in checkpoint metadata
#: so a restore can name them.
EXTERNAL_EFFECT_TOOLS = frozenset(
    {
        "terminal",
        "execute_code",
    }
)

_FAILURE_SIGNATURES = (
    "traceback (most recent call last)",
    "tests failed",
    "test failed",
    "assertionerror",
    "command failed with exit code",
    "npm err!",
    "build failed",
    "compilation failed",
    "syntaxerror",
    "modulenotfounderror",
    "importerror",
    "permission denied",
    "no such file or directory",
    "fatal:",
)

_FAILURE_STATUSES = {"error", "failed", "failure", "blocked", "timeout", "cancelled"}
_NOTICE_TTL_SECONDS = 1800.0
_PENDING_TTL_SECONDS = 1800.0

_PENDING: Dict[Tuple[str, str], Dict[str, Any]] = {}
_NOTICES: Dict[Tuple[str, str], Dict[str, Any]] = {}
_STATE_LOCK = threading.Lock()

_REDACT = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization|bearer|cookie)\b"
    r"(\s*[=:]\s*)(['\"]?)[^\s'\"]+\3"
)
_SPACE = re.compile(r"\s+")


def _pending_key(tool_name: str, kwargs: Dict[str, Any]) -> Tuple[str, str]:
    owner = str(kwargs.get("session_id") or kwargs.get("task_id") or "default")
    call_id = str(kwargs.get("tool_call_id") or "").strip()
    return (owner, call_id or f"tool:{tool_name}")


def _snippet(value: Any, limit: int = 80) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    text = _REDACT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***{m.group(3)}", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _describe(tool_name: str, args: Dict[str, Any]) -> str:
    path = args.get("path") or args.get("file_path") or args.get("target") or args.get("name")
    if tool_name in {"write_file", "patch", "patch_file", "file_edit", "apply_patch", "delete_file"}:
        if path:
            return f"Auto snapshot before {tool_name}: {_snippet(path, 100)}"
        return f"Auto snapshot before {tool_name}"
    if tool_name == "terminal":
        command = args.get("command")
        if command:
            return f"Auto snapshot before terminal: {_snippet(command, 80)}"
        return "Auto snapshot before terminal"
    if tool_name == "execute_code":
        code = args.get("code") or args.get("source")
        if code:
            return f"Auto snapshot before execute_code: {_snippet(code, 60)}"
        return "Auto snapshot before execute_code"
    return f"Auto snapshot before {tool_name}"


def _result_failed(result: Any, status: str = "", error_type: str = "", error_message: str = "") -> Tuple[bool, str]:
    """Classify a tool result.  False positives are worse than misses here."""
    if str(status or "").strip().lower() in _FAILURE_STATUSES:
        return True, f"status={status}"
    if str(error_type or "").strip():
        return True, f"error_type={error_type}"

    if isinstance(result, dict):
        text = json.dumps(result, ensure_ascii=False, default=str)
    else:
        text = "" if result is None else str(result)
    if not text:
        return False, ""

    lowered = text.lower()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        if parsed.get("success") is False or parsed.get("ok") is False:
            return True, "result marked unsuccessful"
        if parsed.get("error"):
            return True, "result contains an error"
        if str(parsed.get("status") or "").strip().lower() in _FAILURE_STATUSES:
            return True, "result status is an error"
        exit_code = parsed.get("exit_code", parsed.get("returncode"))
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return True, f"exit_code={exit_code}"
        if parsed.get("tests_failed") or parsed.get("failed") is True:
            return True, "tests reported failures"

    for signature in _FAILURE_SIGNATURES:
        if signature in lowered:
            return True, f"matched {signature!r}"
    if error_message and str(error_message).strip().lower() in lowered:
        return True, "error_message present"
    return False, ""


def _prune_locked(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for store, ttl in ((_PENDING, _PENDING_TTL_SECONDS), (_NOTICES, _NOTICE_TTL_SECONDS)):
        stale = [key for key, value in store.items() if now - float(value.get("_stored_at", 0)) > ttl]
        for key in stale:
            store.pop(key, None)


class ChronosHooks:
    """Hook callbacks bound to one PluginContext."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.tools = ChronosTools(ctx)

    # -- pre_tool_call ---------------------------------------------------
    def pre_tool_call(self, tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Checkpoint before a modifying tool, or gate a rollback behind approval."""
        try:
            if not isinstance(args, dict):
                args = {}
            # Rollback overwrites workspace files and may delete untracked ones,
            # so the model must not be able to run it unattended.
            if tool_name == ROLLBACK_TOOL:
                return self.rollback_approval(args)
            if tool_name not in MODIFYING_TOOLS:
                return None
            if not _as_bool(_config(self.ctx, "auto_snapshot", True), True):
                return None

            description = _describe(tool_name, args)
            session_id = str(kwargs.get("session_id") or "")
            task_id = str(kwargs.get("task_id") or "")
            turn_id = str(kwargs.get("turn_id") or "")
            # One budget covers engine construction (which may build the shadow
            # repo and write its baseline commit), the slow-workspace check and
            # the checkpoint: Hermes kills the callback at 30 s.
            with git_budget(HOOK_BUDGET_SECONDS):
                engine = self.tools._engine(kwargs)
                workspace = str(getattr(engine, "workspace_root", "") or "")
                if workspace and self._is_slow(workspace):
                    logger.info(
                        "Chronos: skipping auto-snapshot for %s in %s; that workspace was too "
                        "slow to snapshot recently, so the tool runs without a checkpoint",
                        tool_name,
                        workspace,
                    )
                    return None
                checkpoint = engine.create_checkpoint(
                    description,
                    tool_name=tool_name,
                    turn_index=turn_index_for(session_id, task_id, turn_id),
                    session_id=session_id,
                    turn_id=turn_id,
                    task_id=task_id,
                    timeout=HOOK_BUDGET_SECONDS,
                )
            if checkpoint.get("status") == "ok" and checkpoint.get("checkpoint_id"):
                self._clear_slow(workspace)
                key = _pending_key(tool_name, kwargs)
                with _STATE_LOCK:
                    _prune_locked()
                    _PENDING[key] = {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "commit_hash": checkpoint.get("commit_hash", ""),
                        "description": description,
                        "tool_name": tool_name,
                        "_stored_at": time.time(),
                    }
            else:
                self._note_slow(workspace, tool_name, str(checkpoint.get("error") or "unknown error"))
        except Exception:
            # A pre_tool_call exception fails CLOSED in Hermes (the tool is blocked).
            logger.warning("Chronos pre_tool_call hook failed; allowing the tool", exc_info=True)
        return None

    def rollback_approval(self, args: Any) -> Dict[str, Any]:
        """Return Hermes' ``approve`` directive for a model-initiated rollback.

        ``{"action": "approve"}`` is the documented way for a plugin to escalate
        any tool to the human-approval gate; a denial comes back to the model as
        a blocked tool result, and an approval is executed normally.
        """
        args = args if isinstance(args, dict) else {}
        checkpoint = str(
            args.get("checkpoint_id") or args.get("id") or args.get("turn_or_id") or "latest"
        ).strip() or "latest"
        clean = _as_bool(
            args.get("clean"),
            _as_bool(_config(self.ctx, "clean_untracked_on_rollback", True), True),
        )
        effect = (
            "and delete untracked files that are not in the snapshot"
            if clean
            else "and keep untracked files"
        )
        return {
            "action": "approve",
            "message": (
                f"Chronos wants to restore the workspace to checkpoint `{checkpoint}` {effect}. "
                "A safety checkpoint of the current state is taken first, so the file restore "
                "can be undone. Only files under the workspace are restored: database, "
                "service, remote-API, and git-remote changes are not reverted."
            ),
        }

    # -- slow-workspace backoff -----------------------------------------
    def _is_slow(self, workspace: str) -> bool:
        if not workspace:
            return False
        now = time.monotonic()
        with _STATE_LOCK:
            until = _SLOW_WORKSPACES.get(workspace, 0.0)
            if until and until <= now:
                _SLOW_WORKSPACES.pop(workspace, None)
                return False
            return until > now

    def _note_slow(self, workspace: str, tool_name: str, error: str) -> None:
        """Log a failed checkpoint and back off if it keeps missing the budget."""
        if not workspace:
            logger.warning("Chronos could not checkpoint before %s: %s", tool_name, error)
            return
        timed_out = "budget" in error.lower() or "timed out" in error.lower()
        if not timed_out:
            logger.warning("Chronos could not checkpoint before %s: %s", tool_name, error)
            return
        with _STATE_LOCK:
            strikes = _SLOW_STRIKES.get(workspace, 0) + 1
            _SLOW_STRIKES[workspace] = strikes
            pause = strikes >= _SLOW_FAILURE_LIMIT
            if pause:
                _SLOW_STRIKES.pop(workspace, None)
                _SLOW_WORKSPACES[workspace] = time.monotonic() + _SLOW_BACKOFF_SECONDS
        if not pause:
            logger.warning(
                "Chronos missed its %.0fs checkpoint budget before %s (%s); retrying once.",
                HOOK_BUDGET_SECONDS,
                tool_name,
                error,
            )
            return
        logger.warning(
            "Chronos: %s is too large or too slow to checkpoint within %.0fs. Pausing "
            "auto-snapshots there for %.0f minutes; the tool runs without one. Last error: %s",
            workspace,
            HOOK_BUDGET_SECONDS,
            _SLOW_BACKOFF_SECONDS / 60.0,
            error,
        )

    def _clear_slow(self, workspace: str) -> None:
        if not workspace:
            return
        with _STATE_LOCK:
            _SLOW_STRIKES.pop(workspace, None)

    # -- post_tool_call --------------------------------------------------
    def post_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        status: str = "",
        error_type: str = "",
        error_message: str = "",
        **kwargs: Any,
    ) -> None:
        """Classify a result and remember whether to append a recovery hint."""
        try:
            if tool_name not in MODIFYING_TOOLS:
                return None
            key = _pending_key(tool_name, kwargs)
            with _STATE_LOCK:
                pending = _PENDING.pop(key, None)
                _prune_locked()
            if not pending:
                return None
            failed, reason = _result_failed(
                result,
                status=status,
                error_type=error_type,
                error_message=error_message,
            )
            if failed:
                with _STATE_LOCK:
                    _NOTICES[key] = {
                        "checkpoint_id": pending["checkpoint_id"],
                        "reason": reason,
                        "tool_name": tool_name,
                        "_stored_at": time.time(),
                    }
                logger.info(
                    "Chronos: %s failed (%s); checkpoint %s is available",
                    tool_name,
                    reason,
                    pending["checkpoint_id"],
                )
        except Exception:
            logger.debug("Chronos post_tool_call hook failed", exc_info=True)
        return None

    # -- transform_tool_result ------------------------------------------
    def transform_tool_result(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """Append a recovery hint to a failed tool result (Hermes' only transform seam)."""
        try:
            key = _pending_key(tool_name, kwargs)
            with _STATE_LOCK:
                notice = _NOTICES.pop(key, None)
                _prune_locked()
            if not notice:
                return None
            if isinstance(result, str) and "[chronos]" in result.lower():
                return None
            if isinstance(result, dict) and "chronos_notice" in result:
                return None
            checkpoint_id = notice["checkpoint_id"]
            external = notice.get("tool_name") in EXTERNAL_EFFECT_TOOLS
            hint = (
                "\n\n[Chronos] This tool call appears to have failed. "
                f"Checkpoint `{checkpoint_id}` was captured before it ran.\n"
                f"- Inspect the file changes: chronos_diff(from_checkpoint=\"{checkpoint_id}\")\n"
                f"- Restore the workspace files: chronos_rollback(checkpoint_id=\"{checkpoint_id}\")\n"
                "Rollback first creates a safety checkpoint, so the restore is reversible."
            )
            if external:
                hint += (
                    f"\n- Only files under the workspace are restored. `{notice.get('tool_name')}` "
                    "can also change databases, services, remote APIs, git remotes, or files "
                    "outside the workspace, and Chronos cannot see or revert any of that. "
                    "Say what may still be applied instead of reporting the failure as undone."
                )
            if isinstance(result, dict):
                # Hermes 0.19 passes structured tool results to this hook;
                # current Hermes passes a JSON string.
                payload = dict(result)
                payload["chronos_notice"] = hint.strip()
                return json.dumps(payload, ensure_ascii=False, default=str)
            if isinstance(result, list):
                return json.dumps(
                    {"result": result, "chronos_notice": hint.strip()},
                    ensure_ascii=False,
                    default=str,
                )
            if not isinstance(result, str):
                return None
            return result + hint
        except Exception:
            logger.debug("Chronos transform_tool_result hook failed", exc_info=True)
            return None


__all__ = ["ChronosHooks", "MODIFYING_TOOLS", "ROLLBACK_TOOL"]
