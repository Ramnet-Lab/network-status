#!/usr/bin/env bash
# Network Status installer — local (Python venv) or Docker.
# Usage:  ./install.sh            interactive menu
#         ./install.sh local      Python venv install
#         ./install.sh docker     Docker install
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8766}"
OS="$(uname -s)"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }

need() { command -v "$1" >/dev/null 2>&1; }

install_local() {
  say "Local install (Python virtual environment)"
  if ! need python3; then
    err "python3 not found. Install Python 3.10+ and re-run."; exit 1
  fi
  python3 -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  say "Installed. To run:"
  echo "  cd \"$PWD\" && . .venv/bin/activate && python -u monitor.py"
  echo "  then open http://localhost:$PORT"
  printf '\nStart it now? [y/N] '; read -r a
  if [ "${a:-}" = "y" ] || [ "${a:-}" = "Y" ]; then
    say "Starting on http://localhost:$PORT  (Ctrl-C to stop)"
    exec python -u monitor.py
  fi
}

install_docker() {
  say "Docker install"
  if ! need docker; then
    err "docker not found. Install Docker, then re-run."; exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    err "Docker daemon not running. Start Docker and re-run."; exit 1
  fi
  local compose="docker-compose.yml"
  if [ "$OS" != "Linux" ]; then
    warn "Docker Desktop on $OS has no host networking: the container can"
    warn "serve the UI but may NOT reach your 10.2.60.x LAN (pings/battery"
    warn "unreliable). For real checks on $OS, use the local install instead."
    compose="docker-compose.windows.yml"
    printf 'Continue with UI-only Docker on %s? [y/N] ' "$OS"; read -r a
    [ "${a:-}" = "y" ] || [ "${a:-}" = "Y" ] || { echo "Aborted."; exit 0; }
  fi
  # docker compose (v2) or legacy docker-compose
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$compose" up -d --build
  elif need docker-compose; then
    docker-compose -f "$compose" up -d --build
  else
    err "Neither 'docker compose' nor 'docker-compose' available."; exit 1
  fi
  say "Up. Open http://localhost:$PORT"
  echo "  logs:  docker compose -f $compose logs -f"
  echo "  stop:  docker compose -f $compose down"
}

choice="${1:-}"
if [ -z "$choice" ]; then
  say "Network Status installer"
  echo "  1) Local  — Python venv on this machine (best for Mac/Linux on the LAN)"
  echo "  2) Docker — container (host networking on Linux; UI-only on Desktop)"
  printf '\nChoose [1/2]: '; read -r n
  case "$n" in
    1) choice=local ;;
    2) choice=docker ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

case "$choice" in
  local)  install_local ;;
  docker) install_docker ;;
  *) err "Unknown option: $choice (use 'local' or 'docker')"; exit 1 ;;
esac
