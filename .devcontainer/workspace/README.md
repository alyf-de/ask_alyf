# ask_alyf development bench

This workspace is a generated Frappe bench. The `ask_alyf` source checkout is
linked at `apps/ask_alyf`, so edits there are immediately available to the
bench.

The development site is `test_site`. Its Administrator password is
`admin`.

## Start the bench

Choose one startup mode. Do not run `bench start` and a full-stack debug
configuration at the same time because both try to use ports 8000 and 9000.

### Terminal

Run the complete stack without a Python debugger:

```bash
bench start
```

MariaDB and Redis run in separate Compose services; the bench Procfile starts
the web server, Socket.IO, asset watcher, scheduler, and worker.

### VS Code debugger

Open **Run and Debug** and select one of these configurations:

- **Bench: Full stack (debug web)** — recommended for Python debugging. It
  starts the web server under debugpy and starts all non-web Procfile services
  through Honcho.
- **Bench: Web only (stop bench start first)** — starts only the debug web
  server. Use it when the remaining services are already running separately.
- **Bench: Services (without web)** — starts the non-web services only.
- **Bench: Console** — opens an interactive console for `test_site`.
- **Bench: Test ask_alyf** — runs the app test suite under debugpy.
- **Bench: Test module** — prompts for a dotted test module.
- **Bench: Migrate** — migrates `test_site`.
- **Bench: Worker** — runs a worker under debugpy.

Stop the active terminal or debug session before switching startup modes.

## Refresh the environment

The container setup already installs requirements, builds assets, and migrates
the site. To run that workflow again:

```bash
bash /workspace/ask_alyf/.devcontainer/setup.sh
```
