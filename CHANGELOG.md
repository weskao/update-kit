## [0.1.0] - 2026-10-15

### 🚀 Features

- Initial release: `latest_tag`, `install_kind`, `update`, `check`, `hint`, `should_hint`,
  `normalize_lang` — self-update for git-tag-released CLIs installed with uv tool or pipx,
  en + zh-TW messages, cached background check in `$HOME/.update-kit/state.json`

### 🔒 Security

- Repo URL (`url=` / `UPDATE_KIT_REPO_URL`) is validated (`https://`, `ssh://`, `file://`,
  `git@host:path`; no leading `-`, control chars, `ext::` or embedded credentials) and passed to
  `git ls-remote` after `--` with `protocol.ext.allow=never`, so `--upload-pack=...` can no longer
  run code from the background `check()`
- The POSIX install and the Windows detached chain run with the home directory as cwd, so
  `cmd.exe` no longer resolves `uv` / the after-steps from a planted `uv.bat` / `crw.exe` in the
  caller's cwd; `git ls-remote` also runs from home. Only `https`, `ssh`, `file` schemes (lowercase)
  are accepted now (plain `http://` is rejected)
- git stderr is stripped of control characters and URL userinfo before printing; `update` names
  `UPDATE_KIT_REPO_URL` when it is in effect (en + zh-TW)
- Tag parsing: version parts capped at 9 digits, exact `\trefs/tags/` split, no whitespace
  tolerance on tag names; `check()` only returns strict tags
- `check()` caches by resolved source URL, so an override can't poison the default entry
