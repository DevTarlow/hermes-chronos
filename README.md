# ⏱ Hermes Chronos

Time-travel debugging and speculative branching for Hermes Agent.

Chronos takes an isolated filesystem snapshot before every modifying tool call.
When a later step breaks the build, corrupts a config, or deletes a directory,
you can inspect the diff and put the workspace back. The snapshots live in a
private shadow git repository. The project's own `.git`, its branches, commits,
stashes, and hooks are left alone.

Current release: `1.0.0`. Tested on Hermes 0.19.0 and the September 2026 source
tree.

```
write_file / patch / terminal / execute_code
                  │
                  ▼
        ┌───────────────────┐
        │  pre_tool_call    │──► snapshot ──► .hermes/chronos/repo
        └───────────────────┘                  (private git objects)
                  │
                  ▼
             tool runs
                  │
        ┌───────────────────┐
        │  post_tool_call   │──► failure? ──► transform_tool_result
        └───────────────────┘                  "rollback with abc12345"
                  │
                  ▼
     chronos_rollback / /rewind / Desktop pane
                  │
                  ▼
        safety checkpoint ──► restore files ──► workspace
```

## What it does

- **Shadow checkpoints.** A private git object database under
  `.hermes/chronos/repo` uses the workspace as its work tree. It never opens,
  creates, or rewrites the user's `.git`, and it works in projects that are not
  git repositories at all.
- **Automatic snapshots.** `pre_tool_call` snapshots before `write_file`,
  `patch`, `terminal`, `execute_code`, `skill_manage`, and compatible aliases.
  The checkpoint records the tool, turn, session, timestamp, and a short
  description.
- **Recovery hints.** When a tool result looks like a failure,
  `transform_tool_result` appends a short notice naming the checkpoint and the
  exact `chronos_diff` / `chronos_rollback` calls. The model can recover without
  a human in the loop.
- **Reversible rollback.** Restoring a checkpoint first creates a safety
  checkpoint of the current state, so you can restore the pre-rollback state as
  well.
- **Desktop timeline.** A native Hermes Desktop pane (`desktop/plugin.js`)
  lists checkpoints, previews the file diff, and restores with a two-step
  button. The pane polls its scoped backend every five seconds.
- **Interactive `/rewind`.** List, inspect, dry-run, or restore from any Hermes
  surface: CLI, TUI, Desktop, or messaging gateway.
- **Guard rails.** Root and home directories are refused. Ignored paths such as
  `node_modules` and `.venv` are never touched, and a failed snapshot is logged
  instead of blocking the tool.

## Install

From the published repository, pinned to a reviewed commit:

```bash
hermes plugins install DevTarlow/hermes-chronos --ref <40-character-sha>
hermes plugins enable hermes-chronos
```

From a local checkout:

```bash
git clone https://github.com/DevTarlow/hermes-chronos.git ~/.hermes/plugins/hermes-chronos
hermes plugins enable hermes-chronos
```

Hermes installs the declared Python dependencies (`gitpython`, `pydantic`) into
the plugin environment. `git` must be on `PATH`. The engine calls git directly
with an explicit git directory and work tree.

Enable the Desktop half separately in **Capabilities → Plugins → Chronos Time
Travel**. The pane is off until you turn it on, like every third-party Desktop
extension.

## Configuration

Settings live under `plugins.entries.hermes-chronos.settings` in
`~/.hermes/config.yaml`. Current Hermes releases also show them as a form in
**Settings → Plugins**. Hermes 0.19 has no settings form; the plugin reads the
same keys from `config.yaml` directly.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `auto_snapshot` | bool | `true` | Snapshot before modifying tool calls. |
| `max_checkpoints` | int | `50` | Recent checkpoints kept before the shadow repository is compacted with `git gc`. |
| `ignored_patterns` | list | `.git`, `.hermes`, and common dependency/cache directories | Extra gitignore-style patterns. `.git` and `.hermes` are always excluded. |
| `clean_untracked_on_rollback` | bool | `true` | Remove untracked, non-ignored files to match the snapshot. Set false to restore them from the safety checkpoint. |

`ignored_patterns` defaults to `.git`, `.hermes`, `node_modules`, `venv`,
`.venv`, `__pycache__`, and common cache/tool directories.

Example:

```yaml
plugins:
  entries:
    hermes-chronos:
      settings:
        auto_snapshot: true
        max_checkpoints: 80
        ignored_patterns:
          - .git
          - .hermes
          - node_modules
          - dist
        clean_untracked_on_rollback: true
```

## Tools

Every tool returns a JSON string. Success envelopes contain `"status": "ok"`.
Failures contain `"status": "error"` and `"error"`.

| Tool | What it does |
| --- | --- |
| `chronos_snapshot` | Take a checkpoint now. Args: `description` (required), `tool_name`. |
| `chronos_rollback` | Restore a checkpoint. Args: `checkpoint_id`, `clean`, `no_safety`. |
| `chronos_diff` | Diff between checkpoints or the working tree. Args: `from_checkpoint`, `to_checkpoint`, `checkpoint_id`, `max_chars`. |
| `chronos_list_checkpoints` | Recent checkpoints with ids, tools, turns, and timestamps. Arg: `limit`. |

`checkpoint_id` accepts an 8-character id, a full hash, a 1-based index, or
`latest`. With no arguments, `chronos_diff` shows uncommitted changes when the
tree is dirty and the latest checkpoint when the tree is clean.

A rollback response includes the restored id, the safety checkpoint id, the
number of files changed, and the untracked-file counts:

```json
{
  "status": "ok",
  "restored_checkpoint": "1a2b3c4d",
  "safety_checkpoint": "9f8e7d6c",
  "files_changed": 3,
  "restored_files": 41,
  "untracked_removed": 2,
  "untracked_preserved": 0,
  "clean": true
}
```

`untracked_removed` counts files deleted by a `clean: true` restore.
`untracked_preserved` counts files put back by `clean: false`.

## Slash command

```text
/rewind                       list the newest checkpoints
/rewind --list 15             list 15
/rewind 3                     restore the 3rd newest checkpoint
/rewind latest                restore the newest checkpoint
/rewind 1a2b3c4d              restore by id
/rewind --diff 1a2b3c4d       show the files changed by a checkpoint
/rewind --dry-run 3           preview what restoring would undo
/rewind --no-clean 1a2b3c4d   restore tracked files, keep untracked files
```

Rollback always takes a safety checkpoint first. `--dry-run` compares the
selected checkpoint with the current worktree, including changes made after a
previous rollback.

## Desktop pane

`desktop/plugin.js` default-exports a Hermes Desktop plugin and registers a
right-side **Time Travel** pane. It reads the active session's working directory
from `host.state.cwd`, calls the plugin's scoped backend through `ctx.rest`, and
uses `ctx.setInterval` / `ctx.setTimeout` so timers are cleaned up when the
plugin unloads. It imports only `@hermes/plugin-sdk`, `react`, and React hooks.

Selecting a checkpoint loads its diff. **Restore this checkpoint** arms on the
first click and restores on the second. The button disarms after four seconds.

## Backend API

Hermes mounts `dashboard/plugin_api.py` at `/api/plugins/hermes-chronos/`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Plugin metadata and endpoint list. |
| `GET`/`POST` | `/health` | Shadow repo health and statistics. |
| `GET`/`POST` | `/timeline` | Checkpoints plus effective config. |
| `GET`/`POST` | `/diff` | Diff by checkpoint id or between refs. |
| `GET` | `/checkpoint/{id}` | Metadata plus the checkpoint's diff. |
| `POST` | `/rollback` | Restore `{checkpoint_id, cwd?, clean?, create_safety?}`. |

The Desktop half uses the POST forms so the workspace path travels in a JSON
body and stays out of the URL. All endpoints are scoped to this plugin's namespace and
sit behind Hermes' normal dashboard authentication. Long git operations run in
a worker thread so the gateway event loop is not blocked.

`api.py` builds the router. `dashboard/plugin_api.py` loads the engine and route
builder by path and exposes `router`. Hermes imports a dashboard backend by file
path, so the backend cannot rely on package-relative imports.

## How the shadow repository works

1. `git init --bare .hermes/chronos/repo` creates the object database.
2. A private git config sets `core.worktree` to the workspace, disables hooks,
   signing, fsmonitor, and automatic gc, and points `core.excludesFile` at
   `.hermes/chronos/exclude`.
3. Every git command runs with `--git-dir <shadow> --work-tree <workspace>`,
   `GIT_CONFIG_GLOBAL` pointed at the private config, `GIT_CONFIG_NOSYSTEM=1`,
   and a fixed author identity. User aliases and hooks cannot run.
4. `git add -A` stages the workspace. `git diff --cached --quiet HEAD` detects
   no-op snapshots. `git commit` writes a `CHRONOS_CP: {json}` message.
5. Rollback takes a safety checkpoint, then runs
   `git read-tree --reset -u <checkpoint>`. `HEAD` does not move, so the next
   checkpoint is a normal forward commit and no checkpoint becomes unreachable.
6. With `clean: true`, untracked, non-ignored files are removed so the tree
   matches the snapshot. With `clean: false`, those files are restored from the
   safety commit and left untracked.

## Safety and limitations

- **Not a sandbox.** Chronos runs with the user's permissions, like every
  in-process Hermes plugin. It only snapshots and restores files inside the
  session workspace.
- **Ignored files are not snapshotted or restored.** `.gitignore` rules and
  `ignored_patterns` apply. This keeps `node_modules`, virtualenvs, and caches
  out of the shadow repo, but a rollback cannot recreate an ignored file.
- **Empty directories are not tracked by git** and are not recreated.
- **A clean rollback deletes untracked, non-ignored files** to match the
  snapshot exactly. The safety checkpoint captures those files first, so the
  restore can be reversed. Set `clean: false` or use `--no-clean` to keep them.
- **`.hermes/` may show up in your own `git status`.** Chronos excludes it from
  its snapshots but does not edit the project's `.git/info/exclude`. Add
  `.hermes/` to `.gitignore` if you do not want it listed.
- **`max_checkpoints` bounds the listing and triggers compaction.** Git stores
  content-addressed deltas, so a quiet workspace stays small even with a long
  checkpoint list.
- **Git is required.** Snapshot failures are logged and never block a tool.
- **Broad roots are refused.** `/` and the current user's home directory are
  rejected to avoid snapshotting an entire machine.
- **Linux only so far.** The engine uses `pathlib`, `subprocess`, and a POSIX
  file lock with a thread-lock fallback. macOS and Windows have not been run.
- **Desktop rendering is tested outside Electron.** The pane's registration and
  initial render were exercised with React and a stubbed SDK. A visual pass in
  the Desktop app is still worthwhile.

## Disclosures (catalog rule 13)

- Reads files in the active workspace to build checkpoints.
- Writes only under `<workspace>/.hermes/chronos/` and restores files in the
  workspace during an explicit rollback.
- Spawns local `git` subprocesses. No network calls, no telemetry, no remote
  repositories, no shell.
- Runs `git gc` on the shadow repository when history exceeds
  `max_checkpoints + 10`.
- Does not read credentials, `.env` files, or Hermes auth stores.
- Does not update itself. Updates go through Hermes and the catalog pin.

## Validation

```bash
# Same admission gate the catalog CI runs
hermes plugins validate . --install-deps

# Real runtime discovery, manifest parsing, register(ctx), and drift checks
hermes plugins doctor . --ci

# Engine, tool, hook, command, and API tests
python -m unittest discover -s tests -v
```

Expected: `validate` reports every check green with no warnings, `doctor`
reports 4 tools and 3 hooks, and all 23 tests pass.

### Acceptance results

These checks ran against the published repository at the accepted commit, not
against a working tree.

| Check | Result |
| --- | --- |
| Catalog validation | 16/16 checks, 0 warnings |
| Doctor | 4 tools, 3 hooks, no load error |
| Test suite | 23/23 passed |
| Real hook/tool pipeline | 33/33 checks |
| Config behavior | 8/8 checks |
| Dashboard mount and API | 14/14 checks |
| Hermes 0.19.0 compatibility | 11/11 checks |
| Desktop registration and render | 16/16 checks |

The runtime checks cover real `write_file` and `patch` calls through Hermes,
failure-hint injection, rollback, safety-checkpoint undo, `/rewind`, user
`.git` isolation, ignored paths, and JSON error envelopes.

## Catalog submission

The plugin is published at
[https://github.com/DevTarlow/hermes-chronos](https://github.com/DevTarlow/hermes-chronos).
The catalog entry is maintained with the submission files as
`catalog/hermes-chronos.yaml`; it is submitted to `NousResearch/hermes-agent`
under `plugin-catalog/`, not shipped inside the plugin directory.

1. Push the exact commit whose tree is the plugin. The public repository above
   is already at that commit.
2. Set the catalog entry's `sha` to the full 40-character commit. Any later
   change needs a new commit, a new pin, and a re-run of the validation gate.
3. Copy the catalog file to `plugin-catalog/hermes-chronos.yaml` in a fork of
   `NousResearch/hermes-agent` and open a PR.
4. In the PR description, note the shadow-git design, the local git subprocesses,
   the workspace writes, and that the Desktop pane is opt-in.

The catalog entry uses the current schema (`repo`, `sha`, `maintainer`, `tier`,
`category`, `capabilities`) and declares the same `requires_hermes` floor as
`plugin.yaml`.

## Project layout

```text
hermes-chronos/
├── plugin.yaml              # manifest (tools, hooks, deps, config schema)
├── __init__.py              # real Hermes entrypoint: register(ctx)
├── init.py                  # compatibility alias for tooling that expects init.py
├── engine.py                # isolated shadow-git engine
├── tools.py                 # chronos_* tool handlers + JSON contracts
├── hooks.py                 # pre/post/transform lifecycle hooks
├── commands.py              # /rewind
├── api.py                   # FastAPI router builder
├── dashboard/
│   ├── manifest.json        # backend mount declaration
│   └── plugin_api.py        # Hermes mounts this file and its router
├── desktop/
│   └── plugin.js            # native Desktop timeline pane
├── tests/
│   ├── test_chronos.py      # engine, tools, hooks, command
│   └── test_api.py          # dashboard router
├── CHANGELOG.md
├── CONTRIBUTORS.md
├── README.md
└── LICENSE
```

## Implementation notes

The plugin uses the current Hermes surfaces. A few choices differ from the
original sketch:

- Directory plugins load from `__init__.py` and call
  `ctx.register_tool(name=..., toolset=..., schema=..., handler=...)`. Handlers
  receive `(args, **kwargs)`. `init.py` is only a compatibility alias.
- `post_tool_call` is an observer. The failure hint is added through
  `transform_tool_result`, and `provides_hooks` declares all three hooks.
- Dashboard backends mount from `dashboard/plugin_api.py`. `api.py` builds the
  router. There is no `ctx.register_rest_route` API.
- The catalog schema uses `sha` for the exact commit. It does not use the older
  `commit` key.
- Mutations use subprocess git because GitPython derives the work tree from the
  repository location, which is wrong for a separate shadow git directory.
  GitPython is still declared and used for optional read-only introspection.
- Hermes 0.19 passes structured tool results to `transform_tool_result`, and it
  has no `ctx.get_config`. The plugin handles both: structured results get a
  JSON `chronos_notice`, and settings fall back to the active profile's
  `config.yaml`.

## License

MIT. See [LICENSE](LICENSE).
