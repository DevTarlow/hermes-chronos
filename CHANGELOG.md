# Changelog

## 1.0.3 - 2026-11-08

Stop claiming more than a restore delivers: rollback is filesystem-only.

- **`skill_manage` no longer triggers a checkpoint.** It writes to
  `$HERMES_HOME/skills`, outside the workspace, so the snapshot captured nothing
  about the change and restoring it could not undo one — yet a failed
  `skill_manage` still got a "checkpoint captured before it ran / undo it" hint.
  It is no longer in `MODIFYING_TOOLS`.
- **The recovery hint no longer says "Undo it".** It says "restore the workspace
  files", and for `terminal` / `execute_code` — the tools that can touch
  databases, services, remote APIs, git remotes, and files outside the
  workspace — it adds that Chronos cannot see or revert any of that and that the
  failure should not be reported as undone.
- **The approval prompt says the same**, so the decision to restore is made with
  the caveat in view.
- **README** gains "What a rollback does not undo": a table of what survives a
  restore (databases, services, remote APIs, pushed git history, out-of-workspace
  files, packages/caches) and a disclosure line in the catalog section.

Structured per-rollback warnings (`unverified_external_calls` naming the
`terminal` commands in the reverted range) are tracked as a follow-up issue, not
in this release.

## 1.0.2 - 2026-11-05

- **Demo links in the README.** The 88-second launch film
  ([YouTube](https://www.youtube.com/watch?v=s2k0PBo5SO4)) and the short X cut
  ([@MossAIStudio](https://x.com/MossAIStudio/status/2118152188690362610/video/1))
  are linked from the README, which is what the catalog renders. The film was
  recorded against `1.0.0`; the README notes the two moments that predate the
  approval gate and the `/chronos` rename.
- Documentation only, no code changes.

## 1.0.1 - 2026-11-05

Review follow-ups for the Hermes catalog submission.

- **Rollback is no longer unattended for the model.** `chronos_rollback` is
  registered through `pre_tool_call` as an `{"action": "approve"}` directive, so
  Hermes' human-approval gate runs before files are overwritten or untracked
  files are deleted. A denial reaches the model as a blocked tool result.
- **`no_safety` removed from the model tool.** The tool always writes the
  pre-rollback safety checkpoint. The human surfaces keep the flag:
  `/chronos --no-safety` and the dashboard API's `create_safety`.
- **`/rewind` renamed to `/chronos`.** `rewind` is a Hermes core command, so
  `register_command` skipped it silently and the command never existed. The
  handler registers as `/chronos`; `handle_rewind_command` / `make_rewind_handler`
  remain as import aliases.
- **`git gc` moved out of the tool-call path.** Compaction (reflog expire +
  `git gc`) now runs in a background thread once history outgrows
  `max_checkpoints + 10`, and on demand with `/chronos --compact` or
  `ChronosEngine.compact()`. It no longer runs inside `pre_tool_call`.
- **Hook calls are time-bounded.** New `git_budget()` context caps the *whole*
  checkpoint, so a large workspace cannot overrun Hermes' 30 s callback budget
  and fail the tool closed. Auto-snapshots use an 8 s budget; two consecutive
  budget misses put a workspace into a 5-minute slow-mode where snapshots are
  skipped with a log line instead of blocking.
  A stale `index.lock` left by a killed git is cleaned up.
- **The shadow database is hidden from the user's repo.** Init writes
  `.hermes/chronos/.gitignore` containing `*`, so `.hermes/` stops appearing in
  the project's `git status` without editing the user's own ignore files.
- 6 regression tests added (29 total).

## 1.0.0 - 2026-11-04

- Initial release: isolated shadow-git checkpoints, automatic snapshots before modifying tools, failure hints, reversible rollback, `/chronos`, and the Desktop timeline pane.
