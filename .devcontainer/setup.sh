#!/usr/bin/env bash
set -euo pipefail

REPO_NAME="ask_alyf"
REPO_PATH="/workspace/${REPO_NAME}"
BENCH_ROOT="/workspace/frappe-bench"
SITE_NAME="test-site-dev"
FRAPPE_BRANCH="develop"
DB_ROOT_PASSWORD="123"
ADMIN_PASSWORD="admin"

log() { printf '\n\033[1;34m[setup]\033[0m %s\n' "$*"; }

ensure_bench() {
	# The bench lives on a named Docker volume that is root-owned on first
	# mount; the frappe user needs ownership before bench init can write there.
	sudo chown -R frappe:frappe "${BENCH_ROOT}"

	if [ -f "${BENCH_ROOT}/apps/frappe/frappe/__init__.py" ] \
		&& [ -x "${BENCH_ROOT}/env/bin/python" ] \
		&& "${BENCH_ROOT}/env/bin/python" -c "import frappe" 2>/dev/null; then
		log "Bench already initialized at ${BENCH_ROOT}"
		return
	fi

	local contents scratch
	shopt -s dotglob nullglob
	contents=("${BENCH_ROOT}"/*)
	shopt -u dotglob nullglob
	if ((${#contents[@]})); then
		log "Clearing stale bench at ${BENCH_ROOT}"
		rm -rf -- "${contents[@]}"
	fi

	scratch="$(mktemp -d)"
	trap 'rm -rf "${scratch}"' EXIT

	log "Initializing bench with Frappe ${FRAPPE_BRANCH}"
	bench init \
		--frappe-branch "${FRAPPE_BRANCH}" \
		--skip-redis-config-generation \
		--skip-assets \
		"${scratch}/frappe-bench"

	cp -a "${scratch}/frappe-bench/." "${BENCH_ROOT}/"
	rm -rf "${scratch}"
	trap - EXIT

	# Virtual environments are not relocatable: bench init installed Frappe
	# against the scratch path. Recreate it at the final mounted location.
	rm -rf "${BENCH_ROOT}/env"
	uv venv "${BENCH_ROOT}/env" --seed --python python3
	uv pip install --quiet --upgrade -e "${BENCH_ROOT}/apps/frappe" --python "${BENCH_ROOT}/env/bin/python"

	if [ ! -f "${BENCH_ROOT}/apps/frappe/frappe/__init__.py" ] || [ ! -x "${BENCH_ROOT}/env/bin/python" ]; then
		echo "Bench initialization did not produce a usable bench at ${BENCH_ROOT}" >&2
		return 1
	fi
}

configure_bench() {
	log "Configuring bench services"
	cd "${BENCH_ROOT}"
	bench set-config -g db_host mariadb
	bench set-config -g redis_cache "redis://redis-cache:6379/0"
	bench set-config -g redis_queue "redis://redis-queue:6379/1"
	bench set-config -g redis_socketio "redis://redis-queue:6379/2"
	bench set-config -gp webserver_port 8000
	bench set-config -gp socketio_port 9000
	bench set-config -g serve_default_site true
}

install_editor_config() {
	log "Installing bench VS Code configuration"
	mkdir -p "${BENCH_ROOT}/.vscode"
	cp "${REPO_PATH}/.devcontainer/vscode/"*.json "${BENCH_ROOT}/.vscode/"
	cp "${REPO_PATH}/.devcontainer/workspace/README.md" "${BENCH_ROOT}/README.md"
}

link_app() {
	local app_link="${BENCH_ROOT}/apps/${REPO_NAME}"
	if [ -e "${app_link}" ] && [ "$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${app_link}")" != "${REPO_PATH}" ]; then
		rm -rf "${app_link}"
	fi
	if [ ! -e "${app_link}" ]; then
		log "Linking ${REPO_PATH} into bench as apps/${REPO_NAME}"
		bench get-app "${REPO_PATH}" --soft-link
	fi
}

create_site() {
	if [ -d "${BENCH_ROOT}/sites/${SITE_NAME}" ]; then
		log "Site ${SITE_NAME} already exists"
		return
	fi

	log "Creating site ${SITE_NAME}"
	bench new-site \
		--db-root-username root \
		--db-root-password "${DB_ROOT_PASSWORD}" \
		--admin-password "${ADMIN_PASSWORD}" \
		--mariadb-user-host-login-scope "%" \
		"${SITE_NAME}"
}

install_app() {
	bench use "${SITE_NAME}"

	if bench --site "${SITE_NAME}" list-apps --format json \
		| python3 -c 'import json, sys; sys.exit(not any(sys.argv[1] in apps for apps in json.load(sys.stdin).values()))' "${REPO_NAME}"; then
		log "${REPO_NAME} already installed on ${SITE_NAME}"
		return
	fi

	log "Installing ${REPO_NAME} on ${SITE_NAME}"
	bench --site "${SITE_NAME}" install-app "${REPO_NAME}"
}

finalize() {
	log "Installing Python requirements"
	bench setup requirements --dev

	log "Enabling developer mode and tests"
	bench --site "${SITE_NAME}" set-config developer_mode 1
	bench --site "${SITE_NAME}" set-config allow_tests true

	log "Building assets"
	bench build

	log "Running migrations"
	bench --site "${SITE_NAME}" migrate
}

main() {
	log "Python $(python3 --version), Node $(node --version)"

	ensure_bench
	configure_bench
	install_editor_config
	link_app
	create_site
	install_app
	finalize

	log "Done. See ${BENCH_ROOT}/README.md for startup and debugging options."
}

main "$@"
