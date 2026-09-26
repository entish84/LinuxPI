#!/usr/bin/env bash
# ==============================================================================
# Universal Bootstrap Entrypoint for Cross-Distro Setup
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pass help flag directly without requiring sudo
for arg in "$@"; do
    case "${arg}" in
        -h|--help)
            exec python3 "${SCRIPT_DIR}/install.py" "$@"
            ;;
    esac
done

# Check for root / sudo
if [ "${EUID}" -ne 0 ]; then
    echo "Error: This installer must be run with sudo or as root." >&2
    echo "Usage: sudo ./install.sh [OPTIONS]" >&2
    exit 1
fi

# Ensure Python 3 is present
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 not detected. Installing system Python..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq python3
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm python
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/install.py" "$@"
