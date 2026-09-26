#!/usr/bin/env bash
# One-click launcher for the Map + QCC tool pages (Git Bash / MINGW64 friendly).
#
# Usage:
#   ./demo.sh            start the Map(8767) + QCC(8768) tool pages and open the browser
#   ./demo.sh stop       stop the tool pages
#   ./demo.sh full       start the FULL system via start.cmd (requires Docker Desktop)
#
# Any extra flags are forwarded to PowerShell, e.g.:  ./demo.sh -NoBrowser
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PS1="$ROOT/scripts/demo.ps1"

ARGS=()
case "${1:-}" in
    stop) shift; ARGS+=("-Stop") ;;
    full) shift; ARGS+=("-Full") ;;
esac

# Convert the script path to a Windows path for powershell.exe when running under MSYS/Cygwin.
if command -v cygpath >/dev/null 2>&1; then
    WIN_PS1="$(cygpath -w "$PS1")"
else
    WIN_PS1="$PS1"
fi

if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$WIN_PS1" "${ARGS[@]}" "$@"
elif command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -ExecutionPolicy Bypass -File "$PS1" "${ARGS[@]}" "$@"
else
    echo "PowerShell not found. Start the services manually:" >&2
    echo "  python map_and_company/map_integration.py" >&2
    echo "  python map_and_company/company.py" >&2
    exit 1
fi
