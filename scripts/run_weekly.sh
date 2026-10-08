#!/usr/bin/env bash
#
# Weekly entry point for cron. Activates the venv, runs the pipeline, and appends
# everything to logs/cron.log. Exits nonzero if the run failed, so cron's MAILTO
# (or whatever you wrap this in) sees the failure.
#
# Any arguments are passed through to `python -m scout run`, e.g.:
#   scripts/run_weekly.sh --dry-run
#   scripts/run_weekly.sh --since 2026-09-01
#
set -uo pipefail

# Resolve the project directory from this script's own location, so the crontab
# entry does not need to hard-code a path and `cd` is never ambiguous.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd -- "${PROJECT_DIR}" || exit 1

LOG_DIR="${PROJECT_DIR}/logs"
CRON_LOG="${LOG_DIR}/cron.log"
mkdir -p -- "${LOG_DIR}"

VENV_ACTIVATE="${PROJECT_DIR}/.venv/bin/activate"
if [[ ! -f "${VENV_ACTIVATE}" ]]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S%z) FATAL no venv at ${VENV_ACTIVATE} -- run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" \
        | tee -a -- "${CRON_LOG}" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "${VENV_ACTIVATE}"

{
    echo "==============================================================="
    echo "$(date +%Y-%m-%dT%H:%M:%S%z) starting quantph-scout (host $(hostname), TZ=${TZ:-unset}, args: ${*:-none})"
} >> "${CRON_LOG}"

# The pipeline writes its own rotating log to logs/scout.log; this captures
# stderr/stdout as well so a crash before logging is set up is still visible.
python -m scout run "$@" >> "${CRON_LOG}" 2>&1
status=$?

echo "$(date +%Y-%m-%dT%H:%M:%S%z) finished with exit status ${status}" >> "${CRON_LOG}"
exit "${status}"
