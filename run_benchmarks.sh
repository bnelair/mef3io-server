#!/usr/bin/env bash
#
# Run the brainmaze-mef3-server benchmarks using the CURRENTLY ACTIVE
# environment (e.g. an activated conda env). It does not switch envs for you --
# activate the one you want first, for example:
#
#   conda activate bnel-mef3-server
#   ./run_benchmarks.sh benchmark
#
# Usage:
#   ./run_benchmarks.sh [target] [extra pytest args...]
#
# Targets:
#   data         Generate/cache the benchmark dataset only (no timing).
#   micro        In-process cache micro-benchmark (test_file_manager.py).
#   access       Sequential access benchmark (test_access_patterns.py).
#   processing   Detector: native vs gRPC +/- prefetch (test_automated_processing.py).
#   multitool    Many tools, one session: shared cache vs N-way re-decode.
#   benchmark    All pytest-benchmark suites (default).
#   report       Run all benchmark suites, then write a Markdown scenario report.
#   crossover    Heavy crossover-curve analysis; writes benchmark_results/.
#   all          benchmark, then crossover.
#
# Environment:
#   BENCHMARK_CONFIG   Path to an alternate benchmark config JSON.
#
# Examples:
#   ./run_benchmarks.sh benchmark -s
#   ./run_benchmarks.sh benchmark --benchmark-json=results.json
#   BENCHMARK_CONFIG=./my_24h.json ./run_benchmarks.sh all
set -euo pipefail

# Always run from the repository root (this script's directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET="${1:-benchmark}"
shift || true  # remaining args are forwarded to pytest

# Resolve the interpreter of the currently active environment.
PY="$(command -v python3 || command -v python)"
if [[ -z "$PY" ]]; then
  echo "ERROR: no python interpreter found on PATH. Activate your environment first." >&2
  exit 1
fi

echo "=================================================================="
echo " brainmaze-mef3-server benchmarks"
echo "   python : $PY"
echo "   version: $("$PY" --version 2>&1)"
echo "   conda  : ${CONDA_DEFAULT_ENV:-<none>}"
echo "   config : ${BENCHMARK_CONFIG:-tests/benchmark_config.json}"
echo "   target : $TARGET"
echo "=================================================================="

run_pytest() {
  # shellcheck disable=SC2068
  "$PY" -m pytest "$@"
}

case "$TARGET" in
  data)
    "$PY" -m tests.benchmark_data
    ;;
  micro)
    run_pytest -m benchmark tests/test_file_manager.py "$@"
    ;;
  access)
    run_pytest -m benchmark tests/test_access_patterns.py "$@"
    ;;
  processing)
    run_pytest -m benchmark tests/test_automated_processing.py "$@"
    ;;
  multitool)
    run_pytest -m benchmark tests/test_multitool_shared_session.py "$@"
    ;;
  benchmark)
    run_pytest -m benchmark "$@"
    ;;
  report)
    RESULTS_JSON="${BENCHMARK_JSON:-benchmark_results/benchmark.json}"
    mkdir -p "$(dirname "$RESULTS_JSON")"
    run_pytest -m benchmark --benchmark-json="$RESULTS_JSON" "$@"
    "$PY" -m tests.benchmark_report "$RESULTS_JSON"
    ;;
  crossover)
    run_pytest -m crossover "$@"
    ;;
  all)
    run_pytest -m benchmark "$@"
    run_pytest -m crossover "$@"
    ;;
  *)
    echo "ERROR: unknown target '$TARGET'." >&2
    echo "Valid targets: data micro access processing multitool benchmark report crossover all" >&2
    exit 2
    ;;
esac
