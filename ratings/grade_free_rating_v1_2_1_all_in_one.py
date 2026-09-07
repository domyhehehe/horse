#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_rating_v1_2_1_all_in_one.py
======================================

UNIFIED ENTRY POINT for the grade-free annual horse rating pipeline.

Run THIS FILE ONLY.

Stage A
-------
Flat/Jump x Surface hard split rating:
    year x race_type(FLAT/JUMP) x surface_family
    -> Bradley-Terry
    -> opponent-only context
    -> evidence shrink/recenter
    -> dynamic competition networks
    -> atomic per-group checkpoints

Stage B
-------
Race-type-aware realized-breadth calibration:
    -> internal network evidence
    -> venue breadth
    -> opponent-network breadth
    -> horse realized network use
    -> horse cross-network bridge validation
    -> positive-only international release
    -> calibrated Japan rankings

This entry point intentionally keeps the heavy Stage A checkpoint/resume behavior.
If Stage A has already completed, its groups are skipped. Stage B then runs
automatically. You no longer run the two stage scripts separately.

Pyto/iOS write safety
---------------------
The previous breadth script could fail at Path.replace() after writing a .tmp
CSV. This entry point patches breadth CSV output with a defensive writer:
  1) mkdir parent
  2) write same-directory temporary file
  3) verify temporary file exists
  4) os.replace() when possible
  5) if replacement fails, write directly to the final path

Thus breadth output remains rerunnable even on filesystems where atomic replace
is unreliable.

IMPORTANT
---------
This is a unified EXECUTION ENTRY POINT. The tested calculation modules remain
separate internal implementation files in the same folder so the heavy,
validated core is not duplicated and allowed to drift. The user only executes
this file.

Required sibling implementation modules:
- grade_free_multisurface_racetype_dynamic_v1_2_0.py
- grade_free_multisurface_breadth_confidence_v1_2_0.py
(and their existing stable core dependencies)

No grade / series_grade is used by the v1.2 Flat/Jump rating/breadth pipeline.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

import pandas as pd

import grade_free_multisurface_racetype_dynamic_v1_2_0 as rating_stage
import grade_free_multisurface_breadth_confidence_v1_2_0 as breadth_stage

SCRIPT_VERSION = "1.2.1-all-in-one-entry"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = "grade_free_multisurface_results_v1_2_0_flat_jump"


def safe_csv_write(df: pd.DataFrame, path: Path):
    """Pyto/iOS-safe CSV writer with atomic-first, direct-write fallback."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Avoid a fixed .tmp name surviving/crashing across interrupted Pyto runs.
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    try:
        df.to_csv(tmp, index=False, encoding="utf-8-sig")

        # Explicitly verify what Path.replace() previously assumed.
        if not tmp.exists():
            raise FileNotFoundError(
                f"temporary CSV disappeared after write: {tmp}"
            )

        try:
            os.replace(str(tmp), str(path))
            return
        except Exception as replace_exc:
            print(
                f"[write fallback] atomic replace failed for {path.name}: "
                f"{type(replace_exc).__name__}: {replace_exc}",
                flush=True,
            )

        # Breadth is post-processing and fully rerunnable, so direct final write
        # is safer than aborting the whole pipeline on an iOS filesystem quirk.
        df.to_csv(path, index=False, encoding="utf-8-sig")
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def safe_text_write(text: str, path: Path):
    """Small text/report equivalent of the safe CSV writer."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    try:
        tmp.write_text(text, encoding="utf-8")
        if tmp.exists():
            try:
                os.replace(str(tmp), str(path))
                return
            except Exception:
                pass
        path.write_text(text, encoding="utf-8")
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def call_main(module, argv: List[str]):
    """Call an existing tested module.main() with isolated argv."""
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(getattr(module, "__file__", "stage.py"))] + list(argv)
        module.main()
    finally:
        sys.argv = old_argv


def build_stage_a_args(args, output_dir: Path) -> List[str]:
    out = [
        "--base-dir", str(args.base_dir),
        "--output-dir", str(output_dir),
        "--min-valid-year", str(args.min_valid_year),
        "--max-valid-year", str(args.max_valid_year),
    ]
    if args.races:
        out += ["--races", str(args.races)]
    if args.year_start is not None:
        out += ["--year-start", str(args.year_start)]
    if args.year_end is not None:
        out += ["--year-end", str(args.year_end)]
    if args.years:
        out += ["--years", str(args.years)]
    if args.rebuild:
        out += ["--rebuild"]
    return out


def build_stage_b_args(args, output_dir: Path) -> List[str]:
    out = [
        "--base-dir", str(args.base_dir),
        "--results-dir", str(output_dir),
        "--output-dir", str(output_dir),
        "--min-valid-year", str(args.min_valid_year),
        "--max-valid-year", str(args.max_valid_year),
    ]
    if args.races:
        out += ["--races", str(args.races)]
    return out


def verify_stage_a(output_dir: Path):
    required = [
        "grade_free_racetype_surface_dynamic_network_ratings.csv",
        "grade_free_racetype_surface_dynamic_network_summary.csv",
        "grade_free_racetype_surface_horse_ratings.csv",
        "grade_free_racetype_surface_annual_summary.csv",
    ]
    missing = [name for name in required if not (output_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            "Stage A completed without required final outputs: " + ", ".join(missing)
        )


def verify_stage_b(output_dir: Path):
    required = [
        "grade_free_racetype_surface_breadth_calibrated_ratings_v1_2_0.csv",
        "grade_free_japan_racetype_breadth_calibrated_ranking_v1_2_0.csv",
        "grade_free_racetype_breadth_calibration_report_v1_2_0.txt",
    ]
    missing = [name for name in required if not (output_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            "Stage B completed without required final outputs: " + ", ".join(missing)
        )


def write_all_in_one_report(output_dir: Path):
    text = "\n".join([
        "Grade-Free Rating Unified Entry Report",
        f"entry script: {SCRIPT_VERSION}",
        f"rating stage: {getattr(rating_stage, 'SCRIPT_VERSION', 'unknown')}",
        f"breadth stage: {getattr(breadth_stage, 'SCRIPT_VERSION', 'unknown')}",
        "",
        "Pipeline completed:",
        "  Stage A: Flat/Jump x Surface hard-split rating + dynamic networks",
        "  Stage B: race-type-aware realized-breadth calibration",
        "",
        "Execution rule:",
        "  Run grade_free_rating_v1_2_1_all_in_one.py only.",
        "  Stage A checkpoints are retained and reused on rerun.",
        "  Stage B is rerunnable post-processing.",
        "",
        "Pyto write safety:",
        "  breadth CSV writes use atomic-first + direct-write fallback.",
        "",
    ])
    safe_text_write(text, output_dir / "grade_free_all_in_one_report_v1_2_1.txt")


def main():
    ap = argparse.ArgumentParser(
        description="Unified Grade-Free Flat/Jump Multi-Surface rating + breadth pipeline"
    )
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--year-start", type=int, default=None)
    ap.add_argument("--year-end", type=int, default=None)
    ap.add_argument("--years", default=None)
    ap.add_argument("--min-valid-year", type=int, default=1700)
    ap.add_argument("--max-valid-year", type=int, default=__import__("datetime").datetime.now().year)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument(
        "--breadth-only",
        action="store_true",
        help="Skip Stage A and run breadth on already completed v1.2 outputs.",
    )
    args = ap.parse_args()

    args.base_dir = Path(args.base_dir).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else args.base_dir / DEFAULT_OUTPUT_DIR
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    # Patch ONLY output I/O. Mathematical breadth logic stays unchanged.
    breadth_stage.atomic_csv = safe_csv_write

    print("=" * 78, flush=True)
    print(f"Grade-Free Rating ALL-IN-ONE {SCRIPT_VERSION}", flush=True)
    print(f"base   : {args.base_dir}", flush=True)
    print(f"output : {output_dir}", flush=True)
    print("Run this file only. Stage A -> Stage B automatic.", flush=True)
    print("Breadth coefficients unchanged from v1.1.1.", flush=True)
    print("=" * 78, flush=True)

    if not args.breadth_only:
        print("\n[A] Flat/Jump x Surface rating + dynamic networks", flush=True)
        call_main(rating_stage, build_stage_a_args(args, output_dir))
        verify_stage_a(output_dir)
        print("[A] complete", flush=True)
    else:
        print("\n[A] skipped (--breadth-only)", flush=True)
        verify_stage_a(output_dir)

    print("\n[B] Realized breadth calibration", flush=True)
    call_main(breadth_stage, build_stage_b_args(args, output_dir))
    verify_stage_b(output_dir)
    print("[B] complete", flush=True)

    write_all_in_one_report(output_dir)

    print("\n" + "=" * 78, flush=True)
    print("ALL-IN-ONE COMPLETE", flush=True)
    print("Final files of interest:", flush=True)
    for name in [
        "grade_free_japan_racetype_breadth_calibrated_ranking_v1_2_0.csv",
        "grade_free_racetype_surface_breadth_calibrated_ratings_v1_2_0.csv",
        "grade_free_racetype_breadth_calibration_report_v1_2_0.txt",
        "grade_free_all_in_one_report_v1_2_1.txt",
    ]:
        print(f"  {output_dir / name}", flush=True)
    print("=" * 78, flush=True)


if __name__ == "__main__":
    main()
