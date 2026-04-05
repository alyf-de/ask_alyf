# AGENTS.md

## Cursor Cloud specific instructions

This is a **Frappe custom app** (Ask ALYF) that runs inside a Frappe Bench environment. The bench lives at `$HOME/frappe-bench` and the checked-out repo is soft-linked into `$HOME/frappe-bench/apps/ask_alyf`.

### Important environment notes

- **`REPO_NAME` must be set to `ask_alyf`** when running `.cursor/install.sh`, because the workspace directory is `/workspace` and the script defaults `REPO_NAME` to the directory basename. Without this override, the app name becomes "workspace" and everything breaks.
- **PATH must exclude nvm** and include `/usr/local/bin` first so that Node.js 24 (installed to `/usr/local`) is used instead of the nvm-managed Node.js 22. Use: `export PATH="/usr/local/bin:$HOME/.local/bin:$(echo $PATH | tr ':' '\n' | grep -v nvm | tr '\n' ':')"`.
- **Python 3.14** is required (`requires-python = ">=3.14"` in `pyproject.toml`). Install via `uv python install 3.14` and symlink to `/usr/local/bin/python3`.
- The site name is `ask-alyf.localhost` (derived from `REPO_NAME` with underscores replaced by hyphens).

### Starting services

Before running bench, MariaDB and Redis must be up. Run `.cursor/start.sh` to ensure both are running and reachable. Then `cd ~/frappe-bench && bench start`.

### Running tests

```
cd ~/frappe-bench
bench --site ask-alyf.localhost run-tests --app ask_alyf
```

**Known caveat**: `test_ls_find_and_grep_are_scoped_to_installed_app_paths` fails in soft-link mode because symlink resolution causes the app root to resolve outside `apps/`. This test passes in CI where the app is copied rather than symlinked.

### Linting

From the repo root (`/workspace`):

```
~/frappe-bench/env/bin/ruff check .
~/frappe-bench/env/bin/ruff format --check .
```

### Building assets

```
cd ~/frappe-bench && bench build --app ask_alyf
```

The file watcher (`bench start` includes `watch`) auto-rebuilds on JS/CSS changes.

### Login credentials

- URL: `http://ask-alyf.localhost:8000`
- Username: `Administrator`
- Password: `admin`
