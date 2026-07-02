"""Crossover-curve analysis: where does server+prefetch overtake native-local?

This is a heavy, opt-in analysis (NOT part of normal ``pytest`` or ``-m benchmark``
runs -- gate is ``pytest -m crossover``). It sweeps the per-window processing
intensity (``compute_repeats``) and, at each level, times a single cold sequential
detector pass for:

* native local MefReader (in-process baseline),
* gRPC WITH prefetch,
* gRPC WITHOUT prefetch (optional).

The result is written as a JSON + Markdown artifact under ``crossover.output_dir``
so it can be committed and referenced from the README. The story it captures:
with cheap decode (few channels / light compute) native local wins; as per-window
processing grows, server+prefetch hides decode behind compute and overtakes it --
the *crossover point* is the first intensity where prefetch <= native.

A FRESH server is created for every intensity level so each measurement is a cold
single pass (no cache carried over from the previous level), matching how a
detector actually walks a recording once.
"""
import os
import json
import time
import datetime

import pytest
from mef_tools import MefReader

from brainmaze_mef3_server.client import Mef3Client

from .benchmark_data import get_workload, get_crossover, server_kwargs, _resolve_data_dir
from .test_automated_processing import native_processing, grpc_processing


def _time_native(mefd_path, workload):
    rdr = MefReader(mefd_path)
    channels = list(rdr.channels)
    start_uutc = min(rdr.get_property("start_time"))
    t0 = time.perf_counter()
    native_processing(rdr, channels, start_uutc, workload)
    return time.perf_counter() - t0


def _time_grpc(mefd_path, workload, grpc_server_factory, **server_overrides):
    port = grpc_server_factory(**server_kwargs(workload, **server_overrides))
    client = Mef3Client(f"localhost:{port}")
    client.open_file(mefd_path)
    fi = client.get_file_info(mefd_path)
    channels = fi["channel_names"]
    start_uutc = fi["start_uutc"]
    t0 = time.perf_counter()
    grpc_processing(client, mefd_path, channels, start_uutc, workload)
    elapsed = time.perf_counter() - t0
    client.close_file(mefd_path)
    client.shutdown()
    return elapsed


def _write_artifact(output_dir, payload):
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "crossover_curve.json")
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)

    rows = payload["rows"]
    has_np = any(r.get("grpc_no_prefetch_s") is not None for r in rows)
    lines = ["# Crossover curve: native-local vs gRPC+prefetch", ""]
    lines.append(f"- Dataset: {payload['dataset']['channels']} ch, "
                 f"{payload['dataset']['sampling_rate_hz']} Hz, "
                 f"{payload['dataset']['duration_s']} s")
    lines.append(f"- Workload: {payload['num_chunks']} x "
                 f"{payload['segment_size_s']} s windows, "
                 f"mode={payload['processing_mode']}, host CPUs={payload['host_cpu_count']}")
    lines.append(f"- Crossover (first repeats where prefetch <= native): "
                 f"**{payload['crossover_repeats']}**")
    lines.append("")

    columns = ["repeats", "native (s)", "gRPC+prefetch (s)", "speedup vs native"]
    if has_np:
        columns.append("gRPC no-prefetch (s)")

    def _to_row(cells):
        return "| " + " | ".join(cells) + " |"

    lines.append(_to_row(columns))
    lines.append(_to_row(["---:"] * len(columns)))
    for r in rows:
        cells = [
            str(r["compute_repeats"]),
            f"{r['native_s']:.3f}",
            f"{r['grpc_prefetch_s']:.3f}",
            f"{r['prefetch_speedup_vs_native']:.2f}x",
        ]
        if has_np:
            npv = r.get("grpc_no_prefetch_s")
            cells.append(f"{npv:.3f}" if npv is not None else "-")
        lines.append(_to_row(cells))
    md_path = os.path.join(output_dir, "crossover_curve.md")
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return json_path, md_path, "\n".join(lines)


@pytest.mark.crossover
def test_crossover_curve(benchmark_mef3_file, benchmark_config, grpc_server_factory):
    """Sweep processing intensity and record where prefetch overtakes native."""
    workload = dict(get_workload(benchmark_config))
    workload["processing_mode"] = "compute"  # the sweep varies compute intensity
    crossover = get_crossover(benchmark_config)
    sweep = crossover["compute_repeats_sweep"]
    include_np = crossover["include_no_prefetch"]

    rows = []
    for repeats in sweep:
        wl = dict(workload)
        wl["compute_repeats"] = repeats

        native_s = _time_native(benchmark_mef3_file, wl)
        prefetch_s = _time_grpc(benchmark_mef3_file, wl, grpc_server_factory)
        no_prefetch_s = None
        if include_np:
            no_prefetch_s = _time_grpc(
                benchmark_mef3_file, wl, grpc_server_factory,
                prefetch_ahead_windows=0, prefetch_behind_windows=0, max_workers=1,
            )

        row = {
            "compute_repeats": repeats,
            "native_s": native_s,
            "grpc_prefetch_s": prefetch_s,
            "grpc_no_prefetch_s": no_prefetch_s,
            "prefetch_speedup_vs_native": native_s / prefetch_s if prefetch_s else 0.0,
            "prefetch_wins": prefetch_s <= native_s,
        }
        rows.append(row)
        print(f"[crossover] repeats={repeats}: native={native_s:.3f}s "
              f"prefetch={prefetch_s:.3f}s"
              + (f" no_prefetch={no_prefetch_s:.3f}s" if no_prefetch_s else ""))

    crossover_repeats = next(
        (r["compute_repeats"] for r in rows if r["prefetch_wins"]), None
    )
    payload = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dataset": {
            "channels": benchmark_config["channels"],
            "sampling_rate_hz": benchmark_config["sampling_rate_hz"],
            "duration_s": benchmark_config["duration_s"],
            "precision": benchmark_config["precision"],
        },
        "num_chunks": workload["num_chunks"],
        "segment_size_s": workload["segment_size_s"],
        "processing_mode": "compute",
        "host_cpu_count": os.cpu_count(),
        "crossover_repeats": crossover_repeats,
        "rows": rows,
    }

    output_dir = crossover["output_dir"]
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(_resolve_data_dir(benchmark_config), "..", output_dir)
        output_dir = os.path.normpath(output_dir)
    json_path, md_path, table = _write_artifact(output_dir, payload)
    print(f"\n[crossover] wrote {json_path}\n[crossover] wrote {md_path}\n\n{table}\n")

    assert len(rows) == len(sweep)
