"""Hermes Chronos — time-travel checkpoints for Hermes Agent.

Hermes loads a directory plugin through its ``__init__.py`` and calls
``register(ctx)``.  Everything here goes through the official
``ctx.register_*`` surface: no core patching, no monkey-patching, no
``sys.modules`` changes.
"""

import logging

from .commands import make_chronos_handler
from .hooks import ChronosHooks
from .tools import ChronosTools

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-chronos"
PLUGIN_VERSION = "1.0.1"
TOOLSET = "chronos"

SNAPSHOT_SCHEMA = {
    "name": "chronos_snapshot",
    "description": (
        "Create an isolated filesystem checkpoint of the current workspace before a risky "
        "refactor, migration, dependency change, or long shell command. Checkpoints are stored "
        "in a private shadow git database and never touch the project's .git history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short description of the operation about to run.",
            },
            "tool_name": {
                "type": "string",
                "description": "Optional name of the tool or workflow this checkpoint precedes.",
            },
        },
        "required": ["description"],
    },
}

ROLLBACK_SCHEMA = {
    "name": "chronos_rollback",
    "description": (
        "Restore workspace files to a previous Chronos checkpoint after a failed test, broken "
        "build, corrupted config, or bad refactor. A safety checkpoint of the current state is "
        "created first, so the rollback can itself be undone. Hermes asks the user to approve "
        "every rollback before it runs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "Checkpoint id (8 characters, full hash, 1-based index, or 'latest').",
            },
            "clean": {
                "type": "boolean",
                "description": (
                    "Remove untracked, non-ignored files so the workspace matches the snapshot "
                    "(default: the plugin setting, normally true)."
                ),
            },
        },
        "required": ["checkpoint_id"],
    },
}

DIFF_SCHEMA = {
    "name": "chronos_diff",
    "description": (
        "Inspect file changes between Chronos checkpoints, the most recent checkpoint, or the "
        "current uncommitted workspace changes. Use it to understand what a failed step changed "
        "before deciding whether to roll back."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_checkpoint": {
                "type": "string",
                "description": "Base checkpoint id, hash, index, or 'latest'.",
            },
            "to_checkpoint": {
                "type": "string",
                "description": "Target checkpoint id or 'HEAD' / 'WORKTREE' (default: HEAD).",
            },
            "checkpoint_id": {
                "type": "string",
                "description": "Show only the changes introduced by this one checkpoint.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum diff characters to return (default 400000).",
            },
        },
    },
}

LIST_SCHEMA = {
    "name": "chronos_list_checkpoints",
    "description": (
        "List recent Chronos checkpoints with their ids, times, triggering tools, and turn "
        "numbers. Use it to pick a checkpoint for chronos_rollback."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of checkpoints to return (default 15).",
            }
        },
    },
}


def register(ctx):
    """Wire tools, hooks, and the ``/chronos`` command into Hermes."""
    toolset = ChronosTools(ctx)
    hooks = ChronosHooks(ctx)

    ctx.register_tool(
        name="chronos_snapshot",
        toolset=TOOLSET,
        schema=SNAPSHOT_SCHEMA,
        handler=toolset.snapshot,
        emoji="⏱",
    )
    ctx.register_tool(
        name="chronos_rollback",
        toolset=TOOLSET,
        schema=ROLLBACK_SCHEMA,
        handler=toolset.rollback,
        emoji="⏮",
    )
    ctx.register_tool(
        name="chronos_diff",
        toolset=TOOLSET,
        schema=DIFF_SCHEMA,
        handler=toolset.diff,
        emoji="🔍",
    )
    ctx.register_tool(
        name="chronos_list_checkpoints",
        toolset=TOOLSET,
        schema=LIST_SCHEMA,
        handler=toolset.list_checkpoints,
        emoji="🗂",
    )

    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_hook("post_tool_call", hooks.post_tool_call)
    ctx.register_hook("transform_tool_result", hooks.transform_tool_result)

    chronos = make_chronos_handler(ctx)
    try:
        ctx.register_command(
            "chronos",
            chronos,
            description="List, inspect, or restore a Chronos workspace checkpoint.",
            args_hint="[id|index|latest|--list|--diff <id>|--dry-run <id>]",
        )
    except TypeError:
        # Hermes 0.19's register_command predates args_hint; the command is the
        # same, it just renders without an argument hint in older clients.
        ctx.register_command(
            "chronos",
            chronos,
            description="List, inspect, or restore a Chronos workspace checkpoint.",
        )

    logger.info("Hermes Chronos %s registered (tools, hooks, /chronos)", PLUGIN_VERSION)


__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "register"]
