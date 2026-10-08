# Contributing to SSHDesk

Thanks for taking the time to contribute! SSHDesk is a free, community-driven SSH client, and every bug report, idea and pull request helps.

> 🇧🇷 Pode abrir issues e PRs em português também.

## Ways to help

- **Report a bug**: open an [issue](https://github.com/LucasCA-Git/sshdesk/issues/new?template=bug_report.md) with steps to reproduce, your OS and the SSHDesk version (*Help › About*).
- **Suggest a feature**: open a [feature request](https://github.com/LucasCA-Git/sshdesk/issues/new?template=feature_request.md) describing the problem you want to solve.
- **Send a pull request**: fixes, features, docs, translations and tests are all welcome.
- **Security issues**: please **do not** open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

Requirements: Python 3.12+, Git. On Linux/WSL also `sudo apt install python3-venv libxcb-cursor0 libxkbcommon0 libegl1`.

```bash
git clone https://github.com/LucasCA-Git/sshdesk.git
cd sshdesk
python3 -m venv .venv && source .venv/bin/activate      # Windows: py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python app.py --debug
```

## Before opening a pull request

```bash
ruff check src tests          # lint (must pass)
python -m pytest              # unit + offscreen GUI tests (must pass, no SSH server needed)
```

Optional end-to-end tests against real `sshd` instances (run them in a throwaway VM/container — the script changes the root password):

```bash
sudo scripts/start_test_sshd.sh /tmp/sshdesk-sshd
SSHDESK_LIVE_SSHD=/tmp/sshdesk-sshd python -m pytest tests/integration
```

The team server has its own tests: `cd server && pip install -r requirements-dev.txt && python -m pytest`.

### Guidelines

- **The SSH config is the source of truth.** Never store host, user, port or key data anywhere else; UI-only metadata goes to `app_config.json`.
- **Never block the GUI thread.** Network and disk I/O run in worker threads and report back through Qt signals (see `ui/workers.py` and the `_Relay` in `terminal/terminal_session.py`).
- **Never log or persist secrets** in plain text.
- **Add a test** for any logic with a branch, a parser, I/O or security impact. GUI tests run offscreen and must not touch the network.
- Keep changes focused; prefer small PRs. Match the existing style (ruff, type hints, docstrings that explain *why*).
- User-facing text is in English. Docs may be in English or Portuguese.

### Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(sftp): add drag and drop between panels
fix(terminal): ignore callbacks from closed sessions
docs: explain team groups
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`.

## Releasing (maintainers)

1. Bump `__version__` in `src/ssh_terminal/__init__.py` and `version` in `pyproject.toml` (semver).
2. Add the release notes at the top of `src/ssh_terminal/resources/CHANGELOG.md` (English, sections `### New`, `### Improved`, `### Fixed`) and copy the file to the repository root `CHANGELOG.md`. Tests fail if the versions or the two files drift apart.
3. Regenerate screenshots if the UI changed: `QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py`.
4. Commit, tag and push: `git tag v1.2.0 && git push --tags`.

Users see the **What's New** window with the new notes the first time they open the new version.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Be kind and assume good intent.
