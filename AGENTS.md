# Ask ALYF

Ask ALYF is a Frappe Framework app that adds an AI assistant to ERPNext/Frappe Desk. See `README.md` for the product overview, configuration, and tool reference.

## Cursor Cloud specific instructions

The dev environment is defined entirely by committed repo files — treat these as the source of truth and edit them (not a snapshot) when the environment must change:

- `.cursor/environment.json` — wires the Dockerfile, the `install` hook (`.cursor/install.sh`), the `start` hook (`.cursor/start.sh`), and the `bench` terminal.
- `.cursor/Dockerfile` — base image: Python 3.14 + 3.12 via pyenv, Node 24 + 22 via nvm, MariaDB server, Redis server, wkhtmltopdf, and Frappe/PDF build libs. **Hard requirement:** the app needs Python 3.14 and Node 24 (`pyproject.toml` sets `requires-python = ">=3.14"`; Frappe `version-16`'s `package.json` requires `node >=24`). A stock image with older Python/Node will fail.
- `.cursor/install.sh` — creates the bench at `$HOME/frappe-bench`, starts MariaDB/Redis, creates the DB root user, runs `bench init` (Frappe `version-16`), soft-links this repo as the `ask_alyf` app, creates site `ask-alyf.localhost`, installs the app, enables `developer_mode`/`allow_tests`, builds assets, and migrates.

Non-obvious caveats:

- The bench lives at `$HOME/frappe-bench`; the app is **soft-linked** from the repo checkout into `apps/ask_alyf`, so repo edits are live in the bench (no reinstall needed).
- Default site is `ask-alyf.localhost` (derived from the repo name); admin password is `admin`. The DB root user is `frappe`/`frappe`.
- Run the stack from `$HOME/frappe-bench` with `bench start` (web on `:8000`, socketio on `:9000`). The committed `bench` terminal runs `.cursor/start.sh` (starts MariaDB + Redis) then `bench start`. MariaDB/Redis are local services, not external.
- Lint: `pre-commit run --all-files` from the repo root (ruff, ruff-format, prettier, eslint). `commitlint` only runs on the `commit-msg` stage.
- Tests: `bench --site ask-alyf.localhost run-tests --app ask_alyf` (needs `allow_tests` + `developer_mode`, both set by `install.sh`). CI mirrors this in `.github/workflows/ci.yml`.
- **The AI chat requires network egress to `api.openai.com`, which is NOT in the default Cursor Cloud egress allowlist.** Configure the OpenAI key in **Ask ALYF Settings** (`api_key`, `model` e.g. `gpt-4o-mini`, `enabled=1`) or via the `OPENAI_API_KEY` secret. Without the egress allowance the full pipeline runs but the OpenAI Responses API call fails with `openai.APIConnectionError`; the chat then shows the graceful fallback "I hit an error while processing that request." Ask the user to add `api.openai.com` to the environment's egress allowlist for real chat.
