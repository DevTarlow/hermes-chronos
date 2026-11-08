"""Tests for Hermes Chronos.

Run with the plugin's dependency environment:

    python -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    """Load this directory as ``hermes_chronos`` exactly as tests need it."""
    name = "hermes_chronos"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin_package()
from hermes_chronos.engine import (  # noqa: E402
    ChronosEngine,
    ChronosError,
    clear_engine_cache,
    get_engine,
)
from hermes_chronos import hooks as hooks_module  # noqa: E402
from hermes_chronos.hooks import ChronosHooks  # noqa: E402
from hermes_chronos.tools import ChronosTools  # noqa: E402
from hermes_chronos.commands import (  # noqa: E402
    handle_chronos_command,
    handle_rewind_command,
)


class FakeCtx:
    """Minimal stand-in for Hermes' PluginContext."""

    def __init__(self, workspace, config=None):
        self.workspace_dir = str(workspace)
        self.config = dict(config or {})
        self.tools = []
        self.hooks = []
        self.commands = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name=None, **kwargs):
        self.tools.append(name)

    def register_hook(self, name, callback, **kwargs):
        self.hooks.append(name)

    def register_command(self, name, handler, **kwargs):
        self.commands.append((name, handler, kwargs))


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chronos-test-")
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir()
        clear_engine_cache()
        self.engine = ChronosEngine(str(self.workspace))

    def tearDown(self):
        clear_engine_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, relative, content):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_snapshot_and_rollback(self):
        self.write("test.py", "print('version 1')\n")
        cp1 = self.engine.create_checkpoint("Initial version")
        self.assertEqual(cp1["status"], "ok")
        self.assertTrue(cp1["checkpoint_id"])

        self.write("test.py", "print('broken version 2')\n")
        cp2 = self.engine.create_checkpoint("Broken edit")
        self.assertEqual(cp2["status"], "ok")
        self.assertIn("broken version 2", (self.workspace / "test.py").read_text())

        result = self.engine.rollback(cp1["checkpoint_id"])
        self.assertEqual(result["status"], "ok")
        content = (self.workspace / "test.py").read_text()
        self.assertIn("version 1", content)
        self.assertNotIn("broken version 2", content)

    def test_no_changes_returns_existing_checkpoint(self):
        self.write("a.txt", "one\n")
        first = self.engine.create_checkpoint("first")
        second = self.engine.create_checkpoint("second")
        self.assertEqual(second["status"], "ok")
        self.assertFalse(second["created"])
        self.assertEqual(first["checkpoint_id"], second["checkpoint_id"])

    def test_diff_inspection(self):
        self.write("app.js", "console.log('baseline');\n")
        base = self.engine.create_checkpoint("baseline")
        self.write("app.js", "console.log('baseline');\nconsole.log('addition');\n")
        changed = self.engine.create_checkpoint("added line")

        diff = self.engine.get_diff(base["checkpoint_id"], changed["checkpoint_id"])
        self.assertEqual(diff["status"], "ok")
        self.assertIn("+console.log('addition');", diff["diff"])

        checkpoint_diff = self.engine.get_checkpoint_diff(changed["checkpoint_id"])
        self.assertEqual(checkpoint_diff["status"], "ok")
        self.assertIn("+console.log('addition');", checkpoint_diff["diff"])

        # With a clean tree, the no-argument form shows the latest checkpoint.
        default_diff = self.engine.get_diff()
        self.assertEqual(default_diff["status"], "ok")
        self.assertEqual(default_diff["scope"], "last_checkpoint")
        self.assertIn("+console.log('addition');", default_diff["diff"])

        # With uncommitted work, it shows the working-tree changes instead.
        self.write("app.js", "console.log('baseline');\nconsole.log('addition');\nconsole.log('dirty');\n")
        dirty_diff = self.engine.get_diff()
        self.assertEqual(dirty_diff["scope"], "worktree")
        self.assertIn("+console.log('dirty');", dirty_diff["diff"])

    def test_untracked_files_are_removed_on_clean_rollback(self):
        self.write("keep.txt", "keep\n")
        cp = self.engine.create_checkpoint("keep")
        self.write("scratch.txt", "scratch\n")
        result = self.engine.rollback(cp["checkpoint_id"], clean=True)
        self.assertEqual(result["status"], "ok")
        self.assertFalse((self.workspace / "scratch.txt").exists())
        self.assertTrue((self.workspace / "keep.txt").exists())
        self.assertEqual(result.get("untracked_removed"), 1)

    def test_clean_false_preserves_untracked_files(self):
        self.write("keep.txt", "keep\n")
        cp = self.engine.create_checkpoint("keep")
        self.write("scratch.txt", "scratch\n")
        result = self.engine.rollback(cp["checkpoint_id"], clean=False)
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.workspace / "scratch.txt").exists())
        self.assertEqual(result.get("untracked_preserved"), 1)
        self.assertEqual(result.get("untracked_removed"), 0)

    def test_ignored_paths_survive_rollback(self):
        self.write("app.txt", "v1\n")
        cp = self.engine.create_checkpoint("v1")
        self.write("node_modules/keep.txt", "dependency\n")
        result = self.engine.rollback(cp["checkpoint_id"], clean=True)
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.workspace / "node_modules" / "keep.txt").exists())

    def test_rollback_is_reversible_via_safety_checkpoint(self):
        self.write("app.txt", "v1\n")
        cp1 = self.engine.create_checkpoint("v1")
        self.write("app.txt", "v2\n")
        self.engine.create_checkpoint("v2")
        rollback = self.engine.rollback(cp1["checkpoint_id"])
        self.assertEqual(rollback["status"], "ok")
        self.assertEqual((self.workspace / "app.txt").read_text(), "v1\n")
        self.assertTrue(rollback["safety_checkpoint"])

        undo = self.engine.rollback(rollback["safety_checkpoint"])
        self.assertEqual(undo["status"], "ok", undo)
        self.assertEqual((self.workspace / "app.txt").read_text(), "v2\n")

    def test_does_not_touch_user_git_repository(self):
        self.write("tracked.txt", "tracked\n")
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.workspace)],
            check=True,
            env=env,
        )
        subprocess.run(["git", "add", "-A"], cwd=self.workspace, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "user baseline"], cwd=self.workspace, check=True, env=env)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.workspace, check=True, capture_output=True, text=True, env=env
        ).stdout.strip()
        branch_before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

        self.write("tracked.txt", "changed by agent\n")
        cp = self.engine.create_checkpoint("agent edit", tool_name="write_file")
        self.assertEqual(cp["status"], "ok")
        result = self.engine.rollback(cp["checkpoint_id"])
        self.assertEqual(result["status"], "ok")

        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.workspace, check=True, capture_output=True, text=True, env=env
        ).stdout.strip()
        branch_after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()
        self.assertEqual(head_before, head_after)
        self.assertEqual(branch_before, branch_after)

    def test_verify_and_list(self):
        self.write("a.txt", "one\n")
        self.engine.create_checkpoint("one")
        health = self.engine.verify()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["available"])
        checkpoints = self.engine.list_checkpoints(limit=10)
        self.assertGreaterEqual(len(checkpoints), 1)
        self.assertTrue(all("id" in cp and "hash" in cp for cp in checkpoints))

    def test_unknown_checkpoint_is_a_clean_error(self):
        result = self.engine.rollback("deadbeef")
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["error"].lower())

    def test_numeric_and_named_resolution(self):
        self.write("a.txt", "one\n")
        first = self.engine.create_checkpoint("one")
        self.write("a.txt", "two\n")
        second = self.engine.create_checkpoint("two")
        self.assertEqual(self.engine.checkpoint_metadata("latest")["hash"], second["commit_hash"])
        self.assertEqual(self.engine.checkpoint_metadata("1")["hash"], second["commit_hash"])
        self.assertEqual(self.engine.checkpoint_metadata("2")["hash"], first["commit_hash"])

        # An 8-character checkpoint id can be all digits.  It must be treated
        # as a ref first, not as a 12-millionth checkpoint index.
        self.engine._git("branch", "12345678", "HEAD")
        digit_only = self.engine.checkpoint_metadata("12345678")
        self.assertEqual(digit_only["status"], "ok", digit_only)
        self.assertEqual(digit_only["hash"], second["commit_hash"])

    def test_broad_workspace_is_refused(self):
        with self.assertRaises(ChronosError):
            ChronosEngine("/")
        with self.assertRaises(ChronosError):
            ChronosEngine(str(Path.home()))


class ToolsAndHooksTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chronos-tools-")
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir()
        clear_engine_cache()
        self.ctx = FakeCtx(self.workspace)
        self.tools = ChronosTools(self.ctx)
        self.hooks = ChronosHooks(self.ctx)

    def tearDown(self):
        clear_engine_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, relative, content):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_tools_always_return_json(self):
        self.write("a.py", "print(1)\n")
        for payload in (
            self.tools.snapshot({"description": "manual"}),
            self.tools.list_checkpoints({"limit": 5}),
            self.tools.diff({"max_chars": 4000}),
        ):
            parsed = json.loads(payload)
            self.assertEqual(parsed["status"], "ok")

        checkpoint = json.loads(self.tools.list_checkpoints({"limit": 1}))["checkpoints"][0]
        rolled = json.loads(self.tools.rollback({"checkpoint_id": checkpoint["id"]}))
        self.assertEqual(rolled["status"], "ok")

    def test_tool_errors_are_json_not_exceptions(self):
        parsed = json.loads(self.tools.rollback({"checkpoint_id": "does-not-exist"}))
        self.assertEqual(parsed["status"], "error")
        self.assertIn("error", parsed)
        parsed = json.loads(self.tools.diff({"from_checkpoint": "nope"}))
        self.assertEqual(parsed["status"], "error")

    def test_hook_creates_checkpoint_and_adds_failure_hint(self):
        self.write("a.py", "print(1)\n")
        result = self.hooks.pre_tool_call(
            "write_file",
            {"path": "a.py"},
            session_id="session-1",
            task_id="task-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        self.assertIsNone(result)
        checkpoints = json.loads(self.tools.list_checkpoints({"limit": 10}))["checkpoints"]
        self.assertGreaterEqual(len(checkpoints), 1)

        self.hooks.post_tool_call(
            "write_file",
            {"path": "a.py"},
            result=json.dumps({"error": "disk full"}),
            status="error",
            error_type="OSError",
            session_id="session-1",
            task_id="task-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        transformed = self.hooks.transform_tool_result(
            "write_file",
            {"path": "a.py"},
            result=json.dumps({"error": "disk full"}),
            session_id="session-1",
            task_id="task-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        self.assertIsInstance(transformed, str)
        self.assertIn("[Chronos]", transformed)
        self.assertIn("chronos_rollback", transformed)

    def test_external_effect_hint_does_not_promise_a_full_undo(self):
        """A restore is filesystem-only; the hint must say so for terminal/code."""
        notice = {
            "checkpoint_id": "abc12345",
            "reason": "status=error",
            "tool_name": "terminal",
            "_stored_at": time.time(),
        }
        key = ("s-ext", "call-ext")
        with hooks_module._STATE_LOCK:
            hooks_module._NOTICES[key] = notice
        transformed = self.hooks.transform_tool_result(
            "terminal", {"command": "psql -f migrate.sql"}, result="boom",
            session_id="s-ext", tool_call_id="call-ext",
        )
        self.assertIn("Restore the workspace files", transformed)
        self.assertIn("Only files under the workspace are restored", transformed)
        self.assertIn("databases", transformed)
        self.assertNotIn("Undo it", transformed)

    def test_file_only_hint_keeps_the_short_form(self):
        with hooks_module._STATE_LOCK:
            hooks_module._NOTICES[("s-file", "call-file")] = {
                "checkpoint_id": "def67890",
                "reason": "status=error",
                "tool_name": "write_file",
                "_stored_at": time.time(),
            }
        transformed = self.hooks.transform_tool_result(
            "write_file", {"path": "a.py"}, result="boom",
            session_id="s-file", tool_call_id="call-file",
        )
        self.assertIn("Restore the workspace files", transformed)
        self.assertNotIn("databases", transformed)

    def test_skill_manage_is_not_treated_as_a_workspace_tool(self):
        """It writes to $HERMES_HOME/skills, so a checkpoint could not undo it."""
        from hermes_chronos.hooks import MODIFYING_TOOLS

        self.assertNotIn("skill_manage", MODIFYING_TOOLS)
        before = len(json.loads(self.tools.list_checkpoints({"limit": 100}))["checkpoints"])
        self.assertIsNone(
            self.hooks.pre_tool_call(
                "skill_manage", {"name": "deploy"}, session_id="s", tool_call_id="c"
            )
        )
        after = len(json.loads(self.tools.list_checkpoints({"limit": 100}))["checkpoints"])
        self.assertEqual(before, after)
        # And no hint is queued for a failure, because there is no checkpoint to offer.
        self.hooks.post_tool_call(
            "skill_manage", {"name": "deploy"}, result="boom", status="error",
            session_id="s", tool_call_id="c",
        )
        self.assertIsNone(
            self.hooks.transform_tool_result(
                "skill_manage", {"name": "deploy"}, result="boom",
                session_id="s", tool_call_id="c",
            )
        )

    def test_hook_respects_auto_snapshot_false(self):
        self.ctx.config["auto_snapshot"] = False
        before = len(json.loads(self.tools.list_checkpoints({"limit": 100}))["checkpoints"])
        self.hooks.pre_tool_call(
            "write_file",
            {"path": "a.py"},
            session_id="s",
            tool_call_id="c",
            turn_id="t",
        )
        after = len(json.loads(self.tools.list_checkpoints({"limit": 100}))["checkpoints"])
        self.assertEqual(before, after)

    def test_failure_hint_supports_structured_results(self):
        self.write("a.py", "print(1)\n")
        self.hooks.pre_tool_call(
            "patch",
            {"path": "missing.py"},
            session_id="structured",
            tool_call_id="structured-call",
            turn_id="turn-x",
        )
        structured = {"success": False, "error": "missing.py not found"}
        self.hooks.post_tool_call(
            "patch",
            {"path": "missing.py"},
            result=structured,
            status="error",
            session_id="structured",
            tool_call_id="structured-call",
            turn_id="turn-x",
        )
        transformed = self.hooks.transform_tool_result(
            "patch",
            {"path": "missing.py"},
            result=structured,
            session_id="structured",
            tool_call_id="structured-call",
            turn_id="turn-x",
        )
        self.assertIsInstance(transformed, str)
        payload = json.loads(transformed)
        self.assertIn("[Chronos]", payload["chronos_notice"])
        self.assertIn("chronos_rollback", payload["chronos_notice"])

    def test_hook_never_raises_on_bad_payloads(self):
        self.assertIsNone(self.hooks.pre_tool_call("read_file", None))
        self.assertIsNone(self.hooks.pre_tool_call("write_file", None))
        self.assertIsNone(self.hooks.post_tool_call("write_file", None, None))
        self.assertIsNone(self.hooks.transform_tool_result("write_file", None, None))

    def test_chronos_command_lists_and_restores(self):
        self.write("a.txt", "v1\n")
        cp = json.loads(self.tools.snapshot({"description": "v1"}))
        self.write("a.txt", "v2\n")
        self.tools.snapshot({"description": "v2"})

        listing = handle_chronos_command(self.ctx, "")
        self.assertIn("Hermes Chronos", listing)
        self.assertIn(cp["checkpoint_id"], listing)

        restored = handle_chronos_command(self.ctx, cp["checkpoint_id"])
        self.assertIn("restore complete", restored.lower())
        self.assertEqual((self.workspace / "a.txt").read_text(), "v1\n")

        # The worktree can differ from HEAD after a rollback (HEAD may be a
        # safety snapshot), so dry-run must compare against the worktree.
        self.write("a.txt", "v3\n")
        preview = handle_chronos_command(self.ctx, f"--dry-run {cp['checkpoint_id']}")
        self.assertIn("dry run", preview.lower())
        self.assertIn("v3", preview)

    def test_register_wires_the_documented_surface(self):
        registers = []
        commands = []

        class RegisterCtx(FakeCtx):
            def register_tool(self, name=None, **kwargs):
                registers.append(name)

            def register_hook(self, name, callback, **kwargs):
                self.hooks.append(name)

            def register_command(self, name, handler, **kwargs):
                commands.append(name)

        ctx = RegisterCtx(self.workspace)
        PLUGIN.register(ctx)
        self.assertEqual(
            set(registers),
            {"chronos_snapshot", "chronos_rollback", "chronos_diff", "chronos_list_checkpoints"},
        )
        self.assertEqual(
            set(ctx.hooks),
            {"pre_tool_call", "post_tool_call", "transform_tool_result"},
        )
        self.assertEqual(commands, ["chronos"])

    # -- review follow-ups ------------------------------------------------
    def test_rollback_tool_has_no_unattended_no_safety_path(self):
        schema = PLUGIN.ROLLBACK_SCHEMA["parameters"]["properties"]
        self.assertNotIn("no_safety", schema)
        self.assertEqual(set(schema), {"checkpoint_id", "clean"})

        self.write("a.txt", "v1\n")
        cp = json.loads(self.tools.snapshot({"description": "v1"}))
        self.write("a.txt", "v2\n")
        # ``no_safety`` is ignored even if a model still sends it.
        restored = json.loads(
            self.tools.rollback({"checkpoint_id": cp["checkpoint_id"], "no_safety": True})
        )
        self.assertEqual(restored["status"], "ok")
        self.assertTrue(restored["safety_checkpoint"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "v1\n")

    def test_model_rollback_requires_human_approval(self):
        directive = self.hooks.pre_tool_call(
            "chronos_rollback",
            {"checkpoint_id": "abc12345", "clean": True},
            session_id="s",
            tool_call_id="c",
        )
        self.assertIsInstance(directive, dict)
        self.assertEqual(directive["action"], "approve")
        self.assertIn("abc12345", directive["message"])
        self.assertIn("untracked", directive["message"])

        # A non-clean rollback still overwrites files, so it is gated too.
        keep = self.hooks.pre_tool_call("chronos_rollback", {"checkpoint_id": "1", "clean": False})
        self.assertEqual(keep["action"], "approve")
        self.assertIn("keep untracked", keep["message"])

        # Other tools are unaffected by the gate.
        self.assertIsNone(self.hooks.pre_tool_call("read_file", {"path": "a.txt"}))

    def test_shadow_database_is_hidden_from_the_users_repo(self):
        self.write("code.py", "print(1)\n")
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "-A"], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.workspace),
                "-c", "user.name=t", "-c", "user.email=t@example.com",
                "commit", "-qm", "baseline",
            ],
            check=True,
        )

        # First real use builds <workspace>/.hermes/chronos/.
        self.tools.snapshot({"description": "after chronos init"})
        ignore = self.workspace / ".hermes" / "chronos" / ".gitignore"
        self.assertTrue(ignore.is_file())
        self.assertIn("*", ignore.read_text().splitlines())

        self.write("code.py", "print(2)\n")
        self.tools.snapshot({"description": "second"})
        status = subprocess.run(
            ["git", "-C", str(self.workspace), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn(".hermes", status)
        self.assertIn("code.py", status)

    def test_checkpoint_budget_skips_instead_of_hanging(self):
        self.write("a.txt", "v1\n")
        engine = self.tools._engine()
        started = time.time()
        result = engine.create_checkpoint("too slow", timeout=0.2)
        elapsed = time.time() - started
        self.assertEqual(result["status"], "error")
        self.assertIn("budget", result["error"].lower())
        self.assertLess(elapsed, 5.0)

    def test_slow_workspace_backs_off_instead_of_blocking_the_tool(self):
        from hermes_chronos import hooks as hooks_module

        self.write("a.txt", "v1\n")
        self.tools.snapshot({"description": "warm the engine cache"})
        workspace = self.tools._engine().workspace_root

        original = hooks_module.HOOK_BUDGET_SECONDS
        hooks_module.HOOK_BUDGET_SECONDS = 0.05
        try:
            for call in ("c1", "c2"):
                self.assertIsNone(
                    self.hooks.pre_tool_call(
                        "write_file", {"path": "a.txt"}, session_id="s", tool_call_id=call
                    )
                )
            self.assertTrue(self.hooks._is_slow(workspace))
            started = time.time()
            self.assertIsNone(
                self.hooks.pre_tool_call(
                    "write_file", {"path": "a.txt"}, session_id="s", tool_call_id="c3"
                )
            )
            self.assertLess(time.time() - started, 1.0)
        finally:
            hooks_module.HOOK_BUDGET_SECONDS = original
            hooks_module._SLOW_WORKSPACES.clear()
            hooks_module._SLOW_STRIKES.clear()

    def test_compaction_is_scheduled_out_of_band(self):
        engine = self.tools._engine()
        engine.max_checkpoints = 1
        compacted = threading.Event()

        def fake_compact(timeout=None):
            compacted.set()
            return {"status": "ok"}

        engine.compact = fake_compact
        for index in range(13):
            self.write(f"f{index}.txt", f"{index}\n")
            engine.create_checkpoint(f"cp {index}")

        self.assertTrue(compacted.wait(10.0), "background compaction never ran")


if __name__ == "__main__":
    unittest.main()
