"""The ``/chronos`` slash command.

The command is intentionally text-first: slash-command handlers run in the
gateway, and returning a string is what the user sees in CLI, Telegram, the TUI
and Desktop.  No printing, no blocking prompts.
"""

import logging
import shlex
import time
from typing import Any, List, Optional

from .tools import _as_bool, _config, get_engine

logger = logging.getLogger(__name__)

_DEFAULT_LIST = 8
_MAX_DIFF_LINES = 160
_MAX_DIFF_CHARS = 60_000


def _relative_time(timestamp: Any) -> str:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return "unknown time"
    if value <= 0:
        return "unknown time"
    delta = max(0, int(time.time()) - value)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _one_line(value: Any, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "(no description)"


def _format_list(checkpoints: List[dict], workspace: str, limit: int) -> str:
    if not checkpoints:
        return (
            "⏱ **Hermes Chronos** — no checkpoints recorded yet.\n"
            "Chronos snapshots automatically before `write_file`, `patch`, "
            "`terminal`, and `execute_code`. Run `/chronos` again after a tool call."
        )
    lines = [f"⏱ **Hermes Chronos** — {len(checkpoints)} recent checkpoint(s) in `{workspace}`:"]
    for cp in checkpoints[:limit]:
        marker = "•" if not cp.get("is_baseline") else "◦"
        turn = cp.get("turn")
        turn_label = f" · turn {turn}" if isinstance(turn, int) else ""
        lines.append(
            f"{marker} `{cp.get('id', '????????')}` · {_relative_time(cp.get('time'))} · "
            f"{cp.get('tool') or 'manual'}{turn_label} · {_one_line(cp.get('description'))}"
        )
    lines.append("")
    lines.append("Restore: `/chronos <id|index|latest>` · Inspect: `/chronos --diff <id>`")
    lines.append("Preview: `/chronos --dry-run <id>` · List: `/chronos --list`")
    return "\n".join(lines)


def _format_diff(result: dict, checkpoint_id: str) -> str:
    if result.get("status") != "ok":
        return f"❌ **Chronos diff failed** — {result.get('error', 'unknown error')}"
    diff = str(result.get("diff") or "").strip()
    if not diff or diff == "(No modifications)":
        return f"`{checkpoint_id}` has no file modifications to show."
    lines = diff.splitlines()
    if len(lines) > _MAX_DIFF_LINES:
        lines = lines[:_MAX_DIFF_LINES] + ["… [diff truncated]"]
    text = "\n".join(lines)
    if len(text) > _MAX_DIFF_CHARS:
        text = text[:_MAX_DIFF_CHARS] + "\n… [diff truncated]"
    stat = str(result.get("stat") or "").strip()
    header = f"**Chronos diff** for `{checkpoint_id}`"
    if stat:
        header += f"\n```\n{stat}\n```"
    return f"{header}\n```diff\n{text}\n```"


def _help_text() -> str:
    return (
        "⏱ **Hermes Chronos — /chronos**\n"
        "  `/chronos`                     List recent checkpoints\n"
        "  `/chronos --list [n]`          List the newest n checkpoints\n"
        "  `/chronos <id|index|latest>`   Restore the workspace to a checkpoint\n"
        "  `/chronos --dry-run <id>`      Show what restoring would undo\n"
        "  `/chronos --diff <id>`         Show the files changed by a checkpoint\n"
        "  `/chronos --no-clean <id>`     Restore tracked files, keep untracked files\n"
        "  `/chronos --no-safety <id>`    Skip the pre-restore safety checkpoint\n"
        "  `/chronos --compact`           Repack the shadow repository now\n\n"
        "A safety checkpoint is created before every restore, so `/chronos` is reversible."
    )


def _engine_for(ctx: Any, kwargs: dict):
    workspace = kwargs.get("workspace") or kwargs.get("cwd") or kwargs.get("_workspace")
    return get_engine(ctx, workspace=str(workspace) if workspace else None)


def handle_chronos_command(ctx: Any, args: str = "", **kwargs: Any) -> str:
    """Handle ``/chronos [--flags] [turn_or_id]`` and return display text."""
    try:
        raw = str(args or "").strip()
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()

        if tokens and tokens[0].lower() in {"help", "--help", "-h"}:
            return _help_text()

        clean = _as_bool(_config(ctx, "clean_untracked_on_rollback", True), True)
        create_safety = True
        action = "restore"
        target: Optional[str] = None
        list_limit = _DEFAULT_LIST

        index = 0
        while index < len(tokens):
            token = tokens[index]
            lowered = token.lower()
            if lowered in {"--list", "list", "--ls"}:
                action = "list"
                if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                    list_limit = max(1, min(50, int(tokens[index + 1])))
                    index += 1
            elif lowered in {"--diff", "diff"}:
                action = "diff"
                if index + 1 >= len(tokens):
                    return "Usage: `/chronos --diff <checkpoint_id>`"
                target = tokens[index + 1]
                index += 1
            elif lowered in {"--dry-run", "dry-run", "--preview"}:
                action = "dry_run"
                if index + 1 < len(tokens):
                    target = tokens[index + 1]
                    index += 1
            elif lowered in {"--no-clean", "no-clean"}:
                clean = False
            elif lowered in {"--clean", "clean"}:
                clean = True
            elif lowered in {"--no-safety", "no-safety"}:
                # Human surface only: the model tool always keeps the safety net.
                create_safety = False
            elif lowered in {"--compact", "compact", "--gc"}:
                action = "compact"
            elif target is None:
                target = token
            index += 1

        engine = _engine_for(ctx, kwargs)

        if action == "list":
            checkpoints = engine.list_checkpoints(limit=list_limit)
            return _format_list(checkpoints, engine.workspace_root, list_limit)

        if action == "compact":
            result = engine.compact()
            if result.get("status") == "ok":
                return (
                    "🧹 **Chronos compaction complete** — "
                    f"{result.get('commits', '?')} checkpoint(s) in the shadow history."
                )
            return f"❌ **Chronos compaction failed** — {result.get('error', 'unknown error')}"

        if action == "diff":
            if not target:
                latest = engine.list_checkpoints(limit=1)
                if not latest:
                    return "No Chronos checkpoints recorded yet."
                target = str(latest[0]["id"])
            return _format_diff(engine.get_checkpoint_diff(target), target)

        if action == "dry_run":
            if not target:
                return "Usage: `/chronos --dry-run <checkpoint_id|index|latest>`"
            preview = engine.get_diff(target, "WORKTREE")
            if preview.get("status") != "ok":
                return f"❌ **Chronos preview failed** — {preview.get('error', 'unknown error')}"
            diff = str(preview.get("diff") or "").strip()
            if not diff or diff == "(No modifications)":
                return f"Nothing would change: the workspace already matches `{target}`."
            lines = diff.splitlines()
            if len(lines) > _MAX_DIFF_LINES:
                lines = lines[:_MAX_DIFF_LINES] + ["… [diff truncated]"]
            return (
                f"**Chronos dry run** — restoring `{target}` would undo:\n"
                f"```diff\n{chr(10).join(lines)}\n```\n"
                "Run `/chronos "
                f"{target}` to apply it."
            )

        if not target:
            checkpoints = engine.list_checkpoints(limit=list_limit)
            return _format_list(checkpoints, engine.workspace_root, list_limit)

        result = engine.rollback(target, clean=clean, create_safety=create_safety)
        if result.get("status") == "ok":
            safety = result.get("safety_checkpoint")
            safety_note = f" Safety checkpoint: `{safety}`." if safety else ""
            removed = result.get("untracked_removed") or 0
            removed_note = f" Removed {removed} untracked file(s)." if clean and removed else ""
            return (
                f"⏮ **Chronos restore complete** — restored to `{result.get('restored_checkpoint')}`.\n"
                f"{result.get('files_changed', 0)} file(s) changed.{removed_note}{safety_note}\n"
                "Use `/chronos --list` to see the timeline."
            )
        return f"❌ **Chronos restore failed** — {result.get('error', 'unknown error')}"
    except Exception as exc:  # a slash command must always return text
        logger.exception("Chronos /chronos command failed")
        return f"❌ **Chronos restore failed** — {exc}"


def make_chronos_handler(ctx: Any):
    """Return a Hermes-compatible ``(raw_args, **session_context) -> str`` handler."""

    def _handler(raw_args: str = "", **kwargs: Any) -> str:
        return handle_chronos_command(ctx, raw_args, **kwargs)

    return _handler


#: Historical names, kept so older tests/imports keep working.
handle_rewind_command = handle_chronos_command
make_rewind_handler = make_chronos_handler

__all__ = [
    "handle_chronos_command",
    "handle_rewind_command",
    "make_chronos_handler",
    "make_rewind_handler",
]
