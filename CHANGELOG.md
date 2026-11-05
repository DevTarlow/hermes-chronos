# Changelog

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
