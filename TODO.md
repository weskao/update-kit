# TODO

- [ ] Pin the install to the commit SHA seen by `ls-remote` (TOCTOU: a tag can move between check and install)
- [ ] Verify release tag signatures before installing
- [ ] Windows: launch `cmd.exe` (ComSpec) and `git` by absolute path (CreateProcess searches the parent's cwd first)
- [ ] Run `after` steps with cwd=home too; document that an injected `run` must accept `cwd=`
- [x] 0.1.0: frozen public API, tests, CI matrix (macOS / Linux / Windows)
- [ ] Create the GitHub repo, push, tag `v0.1.0` and publish to PyPI (manual hand-off)
- [ ] Add `.github/workflows/release.yml` (PyPI Trusted Publishing) like telegram-kit
- [ ] Verify the Windows detached update chain on a real Windows uv tool install
