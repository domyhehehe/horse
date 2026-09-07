#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_dynamic_v1_0_4_indexed_stream.py
=======================================================

Final optimized launcher for the multi-surface hard-split experiment.

Adds one more optimization on top of v1.0.3:
- build pandas group indices once;
- use groupby.get_group((year, surface_family));
- never scan the full source DataFrame once per group.

All v1.0.3 streaming/checkpoint/finalization helpers are reused.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd

import grade_free_multisurface_dynamic_v1_0_3_stream_checkpoint as opt

core = opt.core
SCRIPT_VERSION = "1.0.4-indexed-stream-checkpoint"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = "grade_free_multisurface_results_v1_0_4"
DEFAULT_MIN_YEAR = 1700
DEFAULT_MAX_YEAR = datetime.now().year


def local_signature(path: Path) -> dict:
    sig = opt.source_signature(path)
    sig["script_version"] = SCRIPT_VERSION
    return sig


def main():
    ap = argparse.ArgumentParser(description="Optimized indexed multi-surface hard-split rating")
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--year-start", type=int, default=None)
    ap.add_argument("--year-end", type=int, default=None)
    ap.add_argument("--years", default=None)
    ap.add_argument("--min-valid-year", type=int, default=DEFAULT_MIN_YEAR)
    ap.add_argument("--max-valid-year", type=int, default=DEFAULT_MAX_YEAR)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    races_path = core.discover_races(base_dir, args.races)
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (base_dir / DEFAULT_OUTPUT_DIR)
    parts_root = out_dir / "_parts"
    manifest_path = out_dir / "checkpoint_manifest.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)

    sig = local_signature(races_path)
    old_sig = None
    if manifest_path.exists():
        try:
            old_sig = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            old_sig = None

    if args.rebuild or (old_sig is not None and old_sig != sig):
        print("Checkpoint invalidated: source/script changed or --rebuild specified.", flush=True)
        if parts_root.exists():
            shutil.rmtree(parts_root)
    parts_root.mkdir(parents=True, exist_ok=True)
    opt.atomic_write_text(json.dumps(sig, ensure_ascii=False, indent=2), manifest_path)

    print("=" * 78, flush=True)
    print(f"Grade-free Multi-Surface Hard Split {SCRIPT_VERSION}", flush=True)
    print(f"input : {races_path}", flush=True)
    print(f"output: {out_dir}", flush=True)
    print("mode  : INDEXED GROUPS + STREAMING PARTS + RESUME CHECKPOINT", flush=True)
    print("grade / series_grade: NOT READ", flush=True)
    print("=" * 78, flush=True)

    df = pd.read_csv(races_path, usecols=core.RACE_USECOLS, dtype=str, low_memory=False)
    source_rows = len(df)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    bad_non_numeric = int(df["year"].isna().sum())
    df = df[df.year.notna()].copy()
    df["year"] = df["year"].astype(int)

    valid_min = int(args.min_valid_year)
    valid_max = int(args.max_valid_year)
    bad_year_mask = (df.year < valid_min) | (df.year > valid_max)
    bad_years = df.loc[bad_year_mask, "year"].value_counts().sort_index().to_dict()
    if bad_years:
        print(f"Ignoring anomalous years outside {valid_min}..{valid_max}: {bad_years}", flush=True)
    df = df[~bad_year_mask].copy()

    exact_years = opt.parse_years(args.years)
    if exact_years is not None:
        exact_years = {y for y in exact_years if valid_min <= y <= valid_max}
        df = df[df.year.isin(exact_years)].copy()
    else:
        if args.year_start is not None:
            df = df[df.year >= max(valid_min, int(args.year_start))].copy()
        if args.year_end is not None:
            df = df[df.year <= min(valid_max, int(args.year_end))].copy()

    filtered_rows = len(df)
    df["_surface_family"] = df["surface"].map(core.surface_family)

    # Critical v1.0.4 optimization: build group indices ONCE.
    grouped = df.groupby(["year", "_surface_family"], sort=True, observed=True)
    group_keys = [(int(y), str(f)) for y, f in grouped.groups.keys()]
    group_keys.sort()

    done_before = sum(opt.completed_group(parts_root, y, f) for y, f in group_keys)
    print(f"groups: {len(group_keys)}  already completed: {done_before}", flush=True)

    for idx, (year, fam) in enumerate(group_keys, start=1):
        gp = opt.group_part_paths(parts_root, year, fam)
        if gp["done"].exists():
            print(f"[{idx}/{len(group_keys)}] {year} {fam}: checkpoint -> SKIP", flush=True)
            continue

        # O(1)-ish indexed lookup instead of rescanning all source rows.
        gdf = grouped.get_group((year, fam)).drop(columns=["_surface_family"])
        race_objs, surface_counts, duplicate_pk_races = core.build_race_objects(gdf)
        print(f"[{idx}/{len(group_keys)}] {year} {fam}: races={len(race_objs)}", flush=True)

        rdf, meta = core.fit_year_surface(year, fam, race_objs)
        if not rdf.empty:
            rdf["surface_reliable"] = (
                (rdf.surface_comparison_evidence >= core.RELIABLE_SURFACE_COMPARISON_EVIDENCE)
                & (rdf.surface_top3_appearances >= core.RELIABLE_SURFACE_TOP3_APPEARANCES)
            ).astype(np.int8)

        if not rdf.empty:
            dhorse, dsum = core.build_dynamic_outputs(year, fam, race_objs, rdf)
        else:
            dhorse, dsum = pd.DataFrame(), pd.DataFrame()

        gp["dir"].mkdir(parents=True, exist_ok=True)
        if not rdf.empty:
            opt.atomic_to_csv(rdf, gp["surface"])
        elif gp["surface"].exists():
            gp["surface"].unlink()
        if not dhorse.empty:
            opt.atomic_to_csv(dhorse, gp["dynamic"])
        elif gp["dynamic"].exists():
            gp["dynamic"].unlink()
        if not dsum.empty:
            opt.atomic_to_csv(dsum, gp["dynamic_summary"])
        elif gp["dynamic_summary"].exists():
            gp["dynamic_summary"].unlink()

        meta2 = dict(meta)
        meta2["duplicate_pk_races"] = int(duplicate_pk_races)
        opt.atomic_to_csv(pd.DataFrame([meta2]), gp["meta"])
        opt.atomic_write_text(f"{SCRIPT_VERSION}\n", gp["done"])
        print(f"    saved checkpoint: {year} {fam}", flush=True)

        del gdf, race_objs, rdf, dhorse, dsum

    # Drop source/group index before finalization to free Pyto memory.
    del grouped
    del df

    outputs = {
        "surface_horse": out_dir / "grade_free_surface_horse_ratings.csv",
        "dynamic_horse": out_dir / "grade_free_surface_dynamic_network_ratings.csv",
        "dynamic_summary": out_dir / "grade_free_surface_dynamic_network_summary.csv",
        "multi": out_dir / "grade_free_horse_multisurface_summary.csv",
        "overlap": out_dir / "grade_free_surface_overlap_matrix.csv",
        "japan": out_dir / "grade_free_japan_surface_ranking.csv",
        "annual_meta": out_dir / "grade_free_surface_annual_summary.csv",
        "report": out_dir / "grade_free_multisurface_report.txt",
    }
    part_map = {(y, f): opt.group_part_paths(parts_root, y, f) for y, f in group_keys}

    print("[final 1/5] assembling raw surface/dynamic CSVs (one part at a time)...", flush=True)
    opt.concat_part_files([part_map[k]["surface"] for k in group_keys if part_map[k]["surface"].exists()], outputs["surface_horse"])
    opt.concat_part_files([part_map[k]["dynamic"] for k in group_keys if part_map[k]["dynamic"].exists()], outputs["dynamic_horse"])
    opt.concat_part_files([part_map[k]["dynamic_summary"] for k in group_keys if part_map[k]["dynamic_summary"].exists()], outputs["dynamic_summary"])
    opt.concat_part_files([part_map[k]["meta"] for k in group_keys if part_map[k]["meta"].exists()], outputs["annual_meta"])
    print("[final 1/5] done", flush=True)

    print("[final 2/5] building multi-surface + Japan summaries year-by-year...", flush=True)
    multi_tmp = outputs["multi"].with_name(outputs["multi"].name + ".tmp")
    japan_tmp = outputs["japan"].with_name(outputs["japan"].name + ".tmp")
    for p in (multi_tmp, japan_tmp):
        if p.exists():
            p.unlink()
    first_multi = True
    first_japan = True
    overlap_acc: Dict[Tuple[str, str], dict] = {}
    by_year = {}
    for k in group_keys:
        by_year.setdefault(k[0], []).append(k)
    years = sorted(by_year)

    for yi, year in enumerate(years, start=1):
        year_keys = by_year[year]
        sframes = [opt.read_if_exists(part_map[k]["surface"]) for k in year_keys if part_map[k]["surface"].exists()]
        sframes = [x for x in sframes if not x.empty]
        if not sframes:
            continue
        surf_year = pd.concat(sframes, ignore_index=True)

        mdf = opt.fast_multisurface_summary(surf_year)
        first_multi = opt.append_df(mdf, multi_tmp, first_multi)
        opt.update_overlap_stats(overlap_acc, surf_year)

        dframes = [opt.read_if_exists(part_map[k]["dynamic"]) for k in year_keys if part_map[k]["dynamic"].exists()]
        dframes = [x for x in dframes if not x.empty]
        if dframes:
            dyn_year = pd.concat(dframes, ignore_index=True)
            jdf = core.build_japan_surface_ranking(surf_year, dyn_year)
            first_japan = opt.append_df(jdf, japan_tmp, first_japan)
            del dyn_year, jdf

        del surf_year, mdf, sframes, dframes
        if yi == 1 or yi == len(years) or yi % 25 == 0:
            print(f"  summary years {yi}/{len(years)}", flush=True)

    if first_multi:
        pd.DataFrame().to_csv(multi_tmp, index=False, encoding="utf-8-sig")
    if first_japan:
        pd.DataFrame().to_csv(japan_tmp, index=False, encoding="utf-8-sig")
    multi_tmp.replace(outputs["multi"])
    japan_tmp.replace(outputs["japan"])
    print("[final 2/5] done", flush=True)

    print("[final 3/5] writing cross-surface overlap matrix...", flush=True)
    opt.atomic_to_csv(opt.overlap_frame(overlap_acc), outputs["overlap"])
    print("[final 3/5] done", flush=True)

    print("[final 4/5] writing report...", flush=True)
    completed = sum(opt.completed_group(parts_root, y, f) for y, f in group_keys)
    report = [
        "グレード非参照・Multi-Surface Hard Split 最適化レポート",
        f"script: {SCRIPT_VERSION}",
        f"core: {getattr(core, 'SCRIPT_VERSION', 'unknown')}",
        f"input: {races_path}",
        f"source rows: {source_rows:,}",
        f"selected rows: {filtered_rows:,}",
        f"non-numeric year rows ignored: {bad_non_numeric:,}",
        f"anomalous years ignored: {bad_years}",
        f"groups: {len(group_keys):,}",
        f"completed groups: {completed:,}",
        "",
        "Optimization:",
        "- source DataFrame group index is created once",
        "- get_group() replaces full-table scan per year x surface",
        "- each completed group is atomically saved immediately",
        "- DONE checkpoint allows resume after interruption",
        "- source/script signature invalidates stale checkpoints",
        "- no all-years result DataFrames are retained in RAM",
        "- final raw CSV assembly reads one part at a time",
        "- multi-surface/Japan summaries are vectorized and year-bounded",
        "- overlap matrix accumulates sufficient statistics only",
        "",
        "Rating semantics are unchanged from v1.0.1 hard-surface split.",
        "TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN are fitted separately.",
        "",
        "If interrupted, run the same command again; completed groups are skipped.",
    ]
    opt.atomic_write_text("\n".join(report) + "\n", outputs["report"])
    print("[final 4/5] done", flush=True)

    print("[final 5/5] complete", flush=True)
    print("Done.", flush=True)
    for key, path in outputs.items():
        if path.exists():
            print(f"  {key}: {path}", flush=True)


if __name__ == "__main__":
    main()
