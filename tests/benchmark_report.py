"""Turn a ``pytest-benchmark`` JSON dump into a human-readable Markdown report.

Each benchmark is presented with (a) a plain-language explanation of the
scenario it measures, (b) which of the three use cases it belongs to, (c) its
timing, and (d) its speedup vs the native-local baseline for that use case.

Usage::

    pytest -m benchmark --benchmark-json=results.json
    python -m tests.benchmark_report results.json            # -> benchmark_results/benchmark_report.md
    python -m tests.benchmark_report results.json --out foo.md

The runner wires this up as ``./run_benchmarks.sh report``.
"""
import os
import sys
import json
import argparse
import datetime


# Use cases (see BENCHMARKS.md). Order controls section order in the report.
USE_CASES = {
    "A": "Use case A — Interactive data viewing (scroll/jump with think-time)",
    "B": "Use case B — Batch: many tools, one session (shared cache)",
    "C": "Use case C — Automated single-pass processing (detector)",
    "infra": "Infrastructure micro-benchmarks (not a user-facing use case)",
}

# A fuller, plain-language explanation of each use case: the real-world
# scenario, what actually costs time, and who should win and why. Printed at the
# top of each section so the numbers below have context.
USE_CASE_INTROS = {
    "A": (
        "**The scenario.** A person is looking at the recording in a viewer or "
        "dashboard. They scroll and jump around, pulling up 10 s–5 min of a "
        "handful of channels at a time, and they revisit places they have "
        "already seen. Between actions there is *think-time* — the human is "
        "reading the trace on screen, not requesting data.\n\n"
        "**What costs time.** Each view needs one decode + (for the server) one "
        "transport; the think-time between views is fixed and dominates the "
        "total. So the levers are: make revisits instant (serve them from cache "
        "instead of re-decoding), and use the idle think-time to pre-load what "
        "the user is likely to open next (prefetch).\n\n"
        "**Who should win.** Native re-reads and re-decodes on every action, "
        "including revisits. The server caches decoded tiles and prefetches "
        "neighbours during think-time. Expect a **rough tie on cold, first-time "
        "views** (think-time swamps the difference); the server pulls ahead as "
        "revisits and prefetch hits accumulate — which this forward-scroll "
        "benchmark does *not* yet exercise (it never revisits), so a tie here is "
        "the honest, conservative result."
    ),
    "B": (
        "**The scenario.** One recorded session must be processed by several "
        "independent tools — e.g. a spike detector, a seizure detector, and a "
        "QC/artifact pass — often as stages of a pipeline. Their data access "
        "*overlaps*: they read the same recording, frequently the same regions.\n\n"
        "**What costs time.** The expensive part of reading MEF3 is decrypt + "
        "decompress (\"decode\"), and it is CPU-bound. With native readers each "
        "tool opens the file itself and decodes independently, so an overlapping "
        "region is decoded **once per tool** — redundant CPU work that scales "
        "with the number of tools. This is exactly the \"I don't want to read + "
        "decrypt/decompress the same data multiple times\" pain point.\n\n"
        "**Who should win.** The **server wins decisively.** One shared tile "
        "cache means the first tool to touch a region pays the decode and every "
        "other tool is served the already-decoded tile — total decode work drops "
        "from once-per-tool to once. The advantage grows with `num_tools` and "
        "with how much the tools' access overlaps (`tool_overlap`); at "
        "`tool_overlap = 0` there is nothing to reuse and the server only adds "
        "transport, which the same benchmark would report honestly."
    ),
    "C": (
        "**The scenario.** A single tool walks the *entire* recording exactly "
        "once, window by window, doing real signal processing on each window (a "
        "detector). No revisiting, and no second consumer.\n\n"
        "**What costs time.** Because nothing is read twice, the cache has "
        "nothing to reuse — the server's only lever is *prefetch*: decode the "
        "next window in the background while the client computes on the current "
        "one, hiding decode behind compute. That only pays off when per-window "
        "compute is large relative to decode **and** decode can actually run "
        "concurrently.\n\n"
        "**Who should win.** Today, **native.** `pymef` decode is GIL-bound and "
        "the benchmark server runs in-process, so prefetch competes for the same "
        "GIL instead of overlapping — the server pays transport + serialization "
        "for no hidden decode. Native (no transport, read→compute serialized) "
        "wins. The *crossover curve* sweeps compute intensity to find where, if "
        "ever, prefetch overtakes; real parallel decode (a process pool) is the "
        "planned change that would tip this toward the server at high channel "
        "counts."
    ),
    "infra": (
        "These are not user-facing use cases — they isolate a single mechanism "
        "(the in-process tile-cache read path, with prefetch on vs off) so "
        "regressions in the core read path are visible without gRPC or workload "
        "noise. On a tiny file with no think-time, prefetch is pure overhead "
        "(background tile reads contend with the foreground read and there is no "
        "idle time to hide behind), so \"prefetch off\" is expected to look "
        "faster here — that is a property of the micro-benchmark, not of the "
        "server under a real workload."
    ),
}

# The native-local baseline benchmark for each use case (for speedup ratios).
BASELINES = {
    "A": "test_baseline_direct_mef_reader",
    "B": "test_multitool_native_local",
    "C": "test_processing_native_local",
}

# name -> (use_case, short title, scenario explanation)
SCENARIOS = {
    # --- Use case A: interactive scroll with think-time --------------------
    "test_baseline_direct_mef_reader": (
        "A", "Native local (baseline)",
        "Scroll forward window-by-window reading directly with MefReader, with a "
        "fixed think-time between windows. No server, no cache, no prefetch.",
    ),
    "test_grpc_sequential_forward_with_prefetch": (
        "A", "gRPC + prefetch",
        "Same forward scroll via the server using get_signal_range; background "
        "tile prefetch loads the next tiles during the think-time so the next "
        "window is already warm.",
    ),
    "test_grpc_sequential_forward_no_prefetch": (
        "A", "gRPC, no prefetch",
        "Same forward scroll via the server but with prefetch off: each window "
        "pays decode + transport with nothing hidden behind think-time.",
    ),
    # --- Use case B: many tools, one session -------------------------------
    "test_multitool_native_local": (
        "B", "Native local (baseline)",
        "N independent tools each process the SAME session locally. Overlapping "
        "regions are decrypted+decompressed once PER TOOL "
        "(num_tools x num_chunks decodes).",
    ),
    "test_multitool_grpc_shared_cache": (
        "B", "gRPC shared tile cache",
        "N tools are clients of ONE server with ONE shared tile cache. The first "
        "tool to touch a region decodes it (cold); every other tool is served "
        "warm — each overlapping region is decoded once, not once per tool.",
    ),
    # --- Use case C: automated single-pass detector ------------------------
    "test_processing_native_local": (
        "C", "Native local (baseline)",
        "One detector walks the whole recording once via MefReader; per-window "
        "read and compute are serialized.",
    ),
    "test_processing_grpc_with_prefetch": (
        "C", "gRPC + prefetch",
        "Same single detector pass via get_signal_range; the server prefetches "
        "upcoming tiles so decode overlaps the client's per-window compute.",
    ),
    "test_processing_grpc_no_prefetch": (
        "C", "gRPC, no prefetch",
        "Same single detector pass via the server with prefetch off: transport "
        "per window, no overlap (the classic 'server slower than MefReader' case).",
    ),
    # --- Infrastructure micro-benchmarks -----------------------------------
    "test_with_prefetch_real_file": (
        "infra", "In-process tile cache, prefetch ON",
        "FileManager.read_signal_range over a few sequential windows, in-process "
        "(no gRPC), with tile prefetch enabled.",
    ),
    "test_no_prefetch_real_file": (
        "infra", "In-process tile cache, prefetch OFF",
        "Same in-process tile-cache access with prefetch disabled — a floor for "
        "how fast the raw read path is with no background help.",
    ),
}


def _classify(name):
    """Return (use_case, title, description) for a benchmark name."""
    if name in SCENARIOS:
        return SCENARIOS[name]
    return ("infra", name, "(no scenario description registered)")


def _fmt_s(seconds):
    return f"{seconds:.3f}"


def _host_line(payload):
    mi = payload.get("machine_info", {})
    cpu = mi.get("cpu", {})
    bits = []
    if cpu.get("brand_raw"):
        bits.append(cpu["brand_raw"])
    if mi.get("system"):
        bits.append(mi["system"])
    # cpu count often lives in each benchmark's extra_info
    for b in payload.get("benchmarks", []):
        n = b.get("extra_info", {}).get("host_cpu_count")
        if n:
            bits.append(f"{n} CPUs")
            break
    return " · ".join(bits) if bits else "unknown host"


def _setup_line(extra):
    """A compact one-liner of the dataset/workload/server setup."""
    parts = []
    if extra.get("channels_under_test") is not None:
        parts.append(f"{extra['channels_under_test']} ch")
    if extra.get("sampling_rate_hz"):
        parts.append(f"{extra['sampling_rate_hz']} Hz")
    if extra.get("file_duration_s") is not None:
        parts.append(f"{extra['file_duration_s']} s file")
    if extra.get("num_chunks") is not None and extra.get("segment_size_s") is not None:
        parts.append(f"{extra['num_chunks']} x {extra['segment_size_s']} s windows")
    if extra.get("num_tools") is not None:
        parts.append(f"num_tools={extra['num_tools']}")
    if extra.get("tool_overlap") is not None:
        parts.append(f"overlap={extra['tool_overlap']}")
    if extra.get("sleep_seconds"):
        parts.append(f"think-time={extra['sleep_seconds']} s")
    if extra.get("compute_repeats") is not None and extra.get("processing_mode") == "compute":
        parts.append(f"compute_repeats={extra['compute_repeats']}")
    if extra.get("n_prefetch") is not None:
        parts.append(f"n_prefetch={extra['n_prefetch']}")
    return ", ".join(parts)


def build_report(payload):
    """Return the Markdown report string for a loaded benchmark JSON payload."""
    benches = {b["name"]: b for b in payload.get("benchmarks", [])}

    lines = ["# Benchmark report", ""]
    generated = payload.get("datetime") or datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    lines.append(f"- Generated: {generated}")
    lines.append(f"- Host: {_host_line(payload)}")
    lines.append(f"- Benchmarks: {len(benches)}")
    lines.append("")
    lines.append("Times are wall-clock seconds (lower is better). *Speedup* is the "
                 "use-case native-local baseline mean divided by this benchmark's "
                 "mean (>1 = faster than native, <1 = slower).")
    lines.append("")

    # --- Summary table (all benchmarks, grouped by use case) ---------------
    lines.append("## Summary")
    lines.append("")
    lines.append("| Use case | Scenario | Mean (s) | Median (s) | StdDev (s) | Speedup vs native |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")

    def _speedup(uc, bench):
        base_name = BASELINES.get(uc)
        base = benches.get(base_name)
        if not base or base["stats"]["mean"] == 0 or bench["stats"]["mean"] == 0:
            return None
        return base["stats"]["mean"] / bench["stats"]["mean"]

    ordered = []  # (uc, name) in section order
    for uc in USE_CASES:
        group = [n for n, b in benches.items() if _classify(n)[0] == uc]
        group.sort(key=lambda n: benches[n]["stats"]["mean"])
        for n in group:
            ordered.append((uc, n))

    for uc, name in ordered:
        bench = benches[name]
        st = bench["stats"]
        _, title, _ = _classify(name)
        sp = _speedup(uc, bench)
        sp_txt = "— (baseline)" if name == BASELINES.get(uc) else (
            f"{sp:.2f}x" if sp is not None else "n/a")
        lines.append(
            f"| {uc} | {title} | {_fmt_s(st['mean'])} | {_fmt_s(st['median'])} "
            f"| {_fmt_s(st['stddev'])} | {sp_txt} |")
    lines.append("")

    # --- Per-use-case detail ----------------------------------------------
    for uc, heading in USE_CASES.items():
        group = [(n, b) for n, b in benches.items() if _classify(n)[0] == uc]
        if not group:
            continue
        group.sort(key=lambda nb: nb[1]["stats"]["mean"])
        lines.append(f"## {heading}")
        lines.append("")
        for name, bench in group:
            _, title, desc = _classify(name)
            st = bench["stats"]
            extra = bench.get("extra_info", {})
            sp = _speedup(uc, bench)
            lines.append(f"### {title}  \n`{name}`")
            lines.append("")
            lines.append(desc)
            lines.append("")
            setup = _setup_line(extra)
            if setup:
                lines.append(f"- Setup: {setup}")
            lines.append(
                f"- Timing: mean **{_fmt_s(st['mean'])} s**, median "
                f"{_fmt_s(st['median'])} s, min {_fmt_s(st['min'])} s, max "
                f"{_fmt_s(st['max'])} s (rounds={st.get('rounds', '?')})")
            if name == BASELINES.get(uc):
                lines.append("- Speedup vs native: — (this is the baseline)")
            elif sp is not None:
                verdict = "faster" if sp >= 1 else "slower"
                lines.append(f"- Speedup vs native: **{sp:.2f}x** ({verdict})")
            lines.append("")

    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", help="pytest-benchmark --benchmark-json output")
    parser.add_argument(
        "--out", default=None,
        help="output Markdown path (default: benchmark_results/benchmark_report.md)")
    args = parser.parse_args(argv)

    with open(args.json_path) as f:
        payload = json.load(f)

    report = build_report(payload)

    out = args.out
    if out is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = os.path.join(repo_root, "benchmark_results", "benchmark_report.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(report)
    print(f"Wrote benchmark report: {out}")
    print()
    print(report)
    return out


if __name__ == "__main__":
    sys.exit(0 if main() else 0)
