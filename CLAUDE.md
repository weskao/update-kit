# CLAUDE.md

## Frozen public API

`update_kit`'s public functions (`latest_tag`, `install_kind`, `update`, `check`, `hint`,
`should_hint`, `normalize_lang`, `UpdateError`) are consumed by aicp, ai-accounts,
codex-reset-watch and agent-toggle. Signatures, defaults, return codes and the
`$HOME/.update-kit/state.json` layout are a contract: changing any of them is a breaking
change (`type(scope)!:` + `BREAKING CHANGE:` footer, minor bump while 0.x).

## Rules

- Stdlib only. No runtime dependencies, no `packaging`.
- Every user-visible string exists in `en` and `zh-TW` in `_MESSAGES`.
- Never prompt; never hit the network in tests (use a `file://` repo in `tmp_path` or a fake `run`).
- Keep `README.md` and `CHANGELOG.md` in sync with user-visible changes.

## Commands

- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Build: `uv build`

## Cross-platform

Code must run on macOS, Linux, and Windows (paths, separators, shell commands, line
endings, `$HOME` vs `%USERPROFILE%`, file locking, symlinks, terminal/ANSI). Guard
platform-specific code and say so.

- Reuse the shared platform helpers first: `src/update_kit/__init__.py` (`_IS_WINDOWS`, `_windows_chain`, `_spawn_detached`).
- Text I/O always passes `encoding="utf-8"` (Windows defaults to cp1252).
- Windows is verified by CI's `windows-latest` job, not locally; POSIX-only tests are
  skipped on Windows (`os.name == "nt"`).
