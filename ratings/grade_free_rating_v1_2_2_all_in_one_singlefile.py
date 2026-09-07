#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_rating_v1_2_2_all_in_one_singlefile.py
================================================

SINGLE-FILE EXECUTION ENTRY for Pyto/iOS.

Put THIS FILE beside stakes_races_one_row.csv and run THIS FILE ONLY.
No sibling grade_free_*.py files are required on the device.

How it works
------------
The validated implementation modules are pinned to one GitHub commit and loaded
DIRECTLY INTO MEMORY.  They are not imported from local sibling .py files.
This avoids ModuleNotFoundError when only this one script is present in Pyto.

Pipeline
--------
Stage A:
  year x race_type(FLAT/JUMP) x surface_family
  -> Bradley-Terry
  -> opponent-only context
  -> evidence shrink/recenter
  -> dynamic competition networks
  -> per-group checkpoints

Stage B:
  -> v1.1.1 realized breadth/confidence calibration
  -> network breadth inheritance control
  -> horse venue breadth
  -> cross-network bridge validation
  -> calibrated Japan rankings

The v1.1.1 breadth coefficients are NOT retuned here.

Pyto write safety
-----------------
Breadth CSV output is patched to:
  mkdir parent -> write tmp -> verify -> os.replace
and if replacement fails, write directly to the final path.

Network requirement
-------------------
Because this is distributed as one small Python file rather than duplicating
thousands of lines of validated implementation code, the pinned implementation
sources are fetched from GitHub into memory at startup.  No extra local Python
files are needed.  The commit is pinned, so later repository edits do not change
this run's calculation logic.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

import pandas as pd

SCRIPT_VERSION = "1.2.2-all-in-one-singlefile"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = "grade_free_multisurface_results_v1_2_0_flat_jump"

# This commit contains all implementation files used by the validated v1.2
# Flat/Jump + v1.1.1 realized-breadth pipeline.
PINNED_COMMIT = "560df1d992e56b7339575a4a092203c64d434302"
RAW_BASE = f"https://raw.githubusercontent.com/domyhehehe/horse/{PINNED_COMMIT}/ratings"

# Dependency order matters.  Every name below is injected into sys.modules
# before the next source is executed, so normal import statements inside the
# implementation resolve from memory instead of local sibling files.
MODULE_FILES: List[tuple[str, str]] = [
    ("grade_free_multisurface_dynamic_v1_0", "grade_free_multisurface_dynamic_v1_0.py"),
    ("grade_free_multisurface_dynamic_v1_0_1", "grade_free_multisurface_dynamic_v1_0_1.py"),
    ("grade_free_multisurface_dynamic_v1_0_3_stream_checkpoint", "grade_free_multisurface_dynamic_v1_0_3_stream_checkpoint.py"),
    ("grade_free_multisurface_racetype_dynamic_v1_2_0", "grade_free_multisurface_racetype_dynamic_v1_2_0.py"),
    ("grade_free_multisurface_breadth_confidence_v1_1_0", "grade_free_multisurface_breadth_confidence_v1_1_0.py"),
    ("grade_free_multisurface_breadth_confidence_v1_2_0", "grade_free_multisurface_breadth_confidence_v1_2_0.py"),
]


def fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "grade-free-rating-singlefile/1.2.2",
            "Accept": "text/plain,*/*;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not load the pinned rating implementation from GitHub. "
            "Check the network connection and run again. "
            f"URL={url} / error={exc}"
        ) from exc
    text = data.decode("utf-8")
    if not text.lstrip().startswith("#!/usr/bin/env python3"):
        raise RuntimeError(f"Unexpected source response from {url}")
    return text


def load_module_from_source(name: str, filename: str):
    # Reuse only a module that THIS entry point already injected.  A stale local
    # module from a prior interactive Pyto run is deliberately replaced.
    sys.modules.pop(name, None)
    url = f"{RAW_BASE}/{filename}"
    source = fetch_text(url)
    mod = types.ModuleType(name)
    mod.__file__ = str(SCRIPT_DIR / filename)
    mod.__package__ = ""
    mod.__loader__ = None
    sys.modules[name] = mod
    try:
        code = compile(source, url, "exec")
        exec(code, mod.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


def load_pipeline_modules() -> Dict[str, object]:
    loaded: Dict[str, object] = {}
    print("Loading pinned internal implementation into memory...", flush=True)
    for i, (name, filename) in enumerate(MODULE_FILES, start=1):
        print(f"  [{i}/{len(MODULE_FILES)}] {filename}", flush=True)
        loaded[name] = load_module_from_source(name, filename)
    return loaded


def safe_csv_writer(df: pd.DataFrame, path: Path):
    """Pyto/iOS defensive CSV writer used to replace breadth.atomic_csv."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass

    # First attempt: same-directory temp + atomic replacement.
    try:
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        if not tmp.exists():
            raise FileNotFoundError(f"temporary CSV disappeared after write: {tmp}")
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except (OSError, AttributeError):
            # Some iOS file providers do not expose fsync cleanly.
            pass
        os.replace(str(tmp), str(path))
        return
    except Exception as first_exc:
        # Clean any remnant and fall back to a direct final-path write.  This is
        # intentionally less atomic but avoids the Path.replace failure observed
        # on Pyto/iOS file-provider paths.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        print(
            f"  [write fallback] {path.name}: atomic replace failed "
            f"({type(first_exc).__name__}: {first_exc})",
            flush=True,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    if not path.exists():
        raise FileNotFoundError(f"CSV write failed: {path}")


def run_stage(main_func, argv: List[str]):
    old_argv = sys.argv[:]
    try:
        sys.argv = argv
        main_func()
    finally:
        sys.argv = old_argv


def parse_args():
    ap = argparse.ArgumentParser(
        description="Single-file Grade-Free Flat/Jump x Surface rating + breadth calibration"
    )
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--year-start", type=int, default=None)
    ap.add_argument("--year-end", type=int, default=None)
    ap.add_argument("--years", default=None)
    ap.add_argument("--min-valid-year", type=int, default=1700)
    ap.add_argument("--max-valid-year", type=int, default=None)
    ap.add_argument(
        "--breadth-only",
        action="store_true",
        help="Skip Stage A and use already completed v1.2 rating outputs.",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Invalidate Stage A checkpoints and recalculate rating groups.",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    base_dir = Path(args.base_dir).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else (base_dir / DEFAULT_OUTPUT_DIR)
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78, flush=True)
    print(f"Grade-Free Rating {SCRIPT_VERSION}", flush=True)
    print("SINGLE FILE: no local sibling grade_free_*.py modules required", flush=True)
    print(f"base   : {base_dir}", flush=True)
    print(f"output : {output_dir}", flush=True)
    print(f"logic  : pinned GitHub commit {PINNED_COMMIT}", flush=True)
    print("=" * 78, flush=True)

    mods = load_pipeline_modules()
    rating_stage = mods["grade_free_multisurface_racetype_dynamic_v1_2_0"]
    breadth_stage = mods["grade_free_multisurface_breadth_confidence_v1_2_0"]

    # Force the known Pyto/iOS temp-file fix even though the pinned breadth
    # implementation itself predates this single-file wrapper.
    breadth_stage.atomic_csv = safe_csv_writer

    common_max_year = args.max_valid_year

    if not args.breadth_only:
        print("\n" + "=" * 78, flush=True)
        print("STAGE A / rating + Flat/Jump + surface + dynamic network", flush=True)
        print("=" * 78, flush=True)
        stage_a = [
            "grade_free_multisurface_racetype_dynamic_v1_2_0.py",
            "--base-dir", str(base_dir),
            "--output-dir", str(output_dir),
            "--min-valid-year", str(args.min_valid_year),
        ]
        if args.races:
            stage_a += ["--races", str(Path(args.races).expanduser())]
        if common_max_year is not None:
            stage_a += ["--max-valid-year", str(common_max_year)]
        if args.year_start is not None:
            stage_a += ["--year-start", str(args.year_start)]
        if args.year_end is not None:
            stage_a += ["--year-end", str(args.year_end)]
        if args.years:
            stage_a += ["--years", str(args.years)]
        if args.rebuild:
            stage_a += ["--rebuild"]
        run_stage(rating_stage.main, stage_a)
    else:
        dyn = output_dir / "grade_free_racetype_surface_dynamic_network_ratings.csv"
        dsum = output_dir / "grade_free_racetype_surface_dynamic_network_summary.csv"
        if not dyn.exists() or not dsum.exists():
            raise FileNotFoundError(
                "--breadth-only was requested but completed Stage A outputs were not found: "
                f"{dyn} / {dsum}"
            )
        print("Stage A skipped (--breadth-only); existing v1.2 outputs found.", flush=True)

    print("\n" + "=" * 78, flush=True)
    print("STAGE B / realized breadth-confidence calibration", flush=True)
    print("=" * 78, flush=True)
    stage_b = [
        "grade_free_multisurface_breadth_confidence_v1_2_0.py",
        "--base-dir", str(base_dir),
        "--results-dir", str(output_dir),
        "--output-dir", str(output_dir),
        "--min-valid-year", str(args.min_valid_year),
    ]
    if args.races:
        stage_b += ["--races", str(Path(args.races).expanduser())]
    if common_max_year is not None:
        stage_b += ["--max-valid-year", str(common_max_year)]
    run_stage(breadth_stage.main, stage_b)

    final_report = output_dir / "grade_free_racetype_breadth_calibration_report_v1_2_0.txt"
    final_rank = output_dir / "grade_free_japan_racetype_breadth_calibrated_ranking_v1_2_0.csv"
    final_rating = output_dir / "grade_free_racetype_surface_breadth_calibrated_ratings_v1_2_0.csv"

    missing = [p for p in (final_report, final_rank, final_rating) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Pipeline finished but expected final output is missing: "
            + ", ".join(str(p) for p in missing)
        )

    print("\n" + "=" * 78, flush=True)
    print("ALL DONE", flush=True)
    print("Final outputs:", flush=True)
    print(f"  {final_report}", flush=True)
    print(f"  {final_rank}", flush=True)
    print(f"  {final_rating}", flush=True)
    print("=" * 78, flush=True)


if __name__ == "__main__":
    main()
