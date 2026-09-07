#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_dynamic_v1_0_3_stream_checkpoint.py
============================================================

Optimized launcher for the multi-surface hard-split experiment.

Main changes from v1.0/v1.0.1:
- Does NOT keep all 500+ year x surface results in memory.
- Saves each completed year x surface group immediately into an atomic part file.
- Resume-safe: completed groups are skipped on the next run.
- If the source CSV or this script version changes, stale parts are discarded.
- Final CSVs are assembled from part files instead of one giant pd.concat.
- Multi-surface summaries are built vectorially, year by year.
- Final Japan rankings are built year by year.
- Surface-overlap statistics are accumulated without loading all years at once.
- Invalid future/anomalous years are filtered by default (1700..current year).
- Small optimization to dynamic partitioning avoids repeated set(comp) creation.

This file imports the tested v1.0 core + v1.0.1 surface normalization.  It changes
execution/storage strategy, not the surface-specific Bradley-Terry/context logic.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

import grade_free_multisurface_dynamic_v1_0_1 as v101

core = v101.core  # v1.0.1 has already patched core.surface_family

SCRIPT_VERSION = "1.0.3-stream-checkpoint"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = "grade_free_multisurface_results_v1_0_3"
DEFAULT_MIN_YEAR = 1700
DEFAULT_MAX_YEAR = datetime.now().year


# ---------------------------------------------------------------------------
# Small core-speed patch: same algorithm, less repeated allocation.
# ---------------------------------------------------------------------------

def fast_dynamic_partition(pair_counts):
    adj, raw_adj = core.build_weighted_adj(pair_counts)
    nodes = sorted(adj)
    parents = core.connected_components(nodes, adj)
    final_groups = []
    parent_map = {}
    parent_sizes = {}

    for parent_id, comp in enumerate(parents, start=1):
        for n in comp:
            parent_map[n] = parent_id
        parent_sizes[parent_id] = len(comp)
        if len(comp) < core.DYNAMIC_MIN_PARENT_HORSES:
            final_groups.append(comp)
            continue

        comp_set = set(comp)  # old code rebuilt this once per node
        sub_adj = {
            n: {nb: w for nb, w in adj.get(n, {}).items() if nb in comp_set}
            for n in comp
        }
        part = core.local_louvain(comp, sub_adj)
        part = core.merge_positive_communities(part, sub_adj)
        part = core.conductance_guard(part, sub_adj)
        groups = defaultdict(list)
        for n, cid in part.items():
            groups[cid].append(n)
        for group in groups.values():
            final_groups.append(sorted(group))

    final_groups.sort(key=lambda xs: (-len(xs), min(xs)))
    net_map = {}
    for net_id, group in enumerate(final_groups, start=1):
        for n in group:
            net_map[n] = net_id
    return (
        net_map,
        parent_map,
        parent_sizes,
        adj,
        raw_adj,
        core.partition_modularity(net_map, adj),
    )


core.dynamic_partition = fast_dynamic_partition


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def parse_years(text: Optional[str]) -> Optional[set]:
    if not text:
        return None
    out = set()
    for token in str(text).split(","):
        token = token.strip()
        if token:
            out.add(int(token))
    return out or None


def source_signature(path: Path) -> dict:
    """Fast-but-strong signature: size + mtime + SHA256(head+tail 1 MiB)."""
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(st.st_size).encode())
    block = 1024 * 1024
    with path.open("rb") as f:
        head = f.read(block)
        h.update(head)
        if st.st_size > block:
            f.seek(max(0, st.st_size - block))
            h.update(f.read(block))
    return {
        "resolved_path": str(path.resolve()),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "edge_sha256": h.hexdigest(),
        "script_version": SCRIPT_VERSION,
        "core_version": getattr(core, "SCRIPT_VERSION", "unknown"),
    }


def atomic_to_csv(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)


def atomic_write_text(text: str, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def append_df(df: pd.DataFrame, path: Path, first: bool) -> bool:
    if df is None or df.empty:
        return first
    df.to_csv(
        path,
        mode="w" if first else "a",
        header=first,
        index=False,
        encoding="utf-8-sig" if first else "utf-8",
    )
    return False


def part_dir(parts_root: Path, year: int, family: str) -> Path:
    return parts_root / f"{int(year):04d}" / family


def completed_group(parts_root: Path, year: int, family: str) -> bool:
    return (part_dir(parts_root, year, family) / "DONE").exists()


def group_part_paths(parts_root: Path, year: int, family: str) -> dict:
    d = part_dir(parts_root, year, family)
    return {
        "dir": d,
        "surface": d / "surface_horse.csv",
        "dynamic": d / "dynamic_horse.csv",
        "dynamic_summary": d / "dynamic_summary.csv",
        "meta": d / "annual_meta.csv",
        "done": d / "DONE",
    }


def read_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def concat_part_files(paths: Iterable[Path], output: Path):
    """Memory bounded: one part DataFrame at a time."""
    tmp = output.with_name(output.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    first = True
    for p in paths:
        df = read_if_exists(p)
        if df.empty:
            continue
        first = append_df(df, tmp, first)
    if first:
        pd.DataFrame().to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(output)


# ---------------------------------------------------------------------------
# Vectorized final horse summary (replaces Python group loop).
# ---------------------------------------------------------------------------

def fast_multisurface_summary(surface_df: pd.DataFrame) -> pd.DataFrame:
    if surface_df.empty:
        return pd.DataFrame()

    df = surface_df.copy()
    rel = (
        (df["surface_comparison_evidence"] >= core.RELIABLE_SURFACE_COMPARISON_EVIDENCE)
        & (df["surface_top3_appearances"] >= core.RELIABLE_SURFACE_TOP3_APPEARANCES)
    )
    df["_reliable"] = rel.astype(np.int8)

    keys = ["year", "horse_pk"]
    group = df.groupby(keys, sort=False)
    rating_count = group.size().rename("surface_rating_count")
    reliable_count = group["_reliable"].sum().rename("reliable_surface_count")
    total_top3 = group["surface_top3_appearances"].sum().rename("total_surface_top3_appearances")

    # Primary: highest reliable surface if any; otherwise highest available surface.
    primary = (
        df.sort_values(
            keys + ["_reliable", "annual_surface_rating", "surface_family"],
            ascending=[True, True, False, False, True],
            kind="mergesort",
        )
        .drop_duplicates(keys, keep="first")
        .set_index(keys)
    )

    reliable_rows = df[df["_reliable"] == 1]
    reliable_floor = (
        reliable_rows.groupby(keys, sort=False)["annual_surface_rating"]
        .min()
        .rename("multi_surface_floor_relative")
    )

    base = pd.concat([rating_count, reliable_count, total_top3], axis=1)
    base = base.join(
        primary[[
            "horse_name", "surface_family", "annual_surface_rating",
            "surface_comparison_evidence", "surface_top3_appearances",
        ]].rename(columns={
            "surface_family": "primary_surface_family",
            "annual_surface_rating": "primary_strength_relative",
            "surface_comparison_evidence": "primary_surface_comparison_evidence",
            "surface_top3_appearances": "primary_surface_top3_appearances",
        }),
        how="left",
    )
    base = base.join(reliable_floor, how="left")
    base["multi_surface_verified"] = (base["reliable_surface_count"] >= 2).astype(np.int8)
    base.loc[base["reliable_surface_count"] < 2, "multi_surface_floor_relative"] = np.nan
    base["primary_minus_floor"] = (
        base["primary_strength_relative"] - base["multi_surface_floor_relative"]
    )

    # Effective number of surfaces from Top3 distribution: exp(entropy).
    totals = df.groupby(keys, sort=False)["surface_top3_appearances"].transform("sum")
    p = df["surface_top3_appearances"].astype(float) / totals.replace(0, np.nan)
    entropy_term = -(p * np.log(p.where(p > 0)))
    entropy = entropy_term.groupby([df["year"], df["horse_pk"]]).sum(min_count=1)
    base["effective_top3_surface_count"] = np.exp(entropy).reindex(base.index).fillna(0.0)

    # Wide surface columns. One row per year-horse-surface is expected.
    wide = df.pivot_table(
        index=keys,
        columns="surface_family",
        values=[
            "annual_surface_rating", "surface_rank",
            "surface_comparison_evidence", "surface_top3_appearances",
        ],
        aggfunc="first",
    )

    for fam in core.SURFACE_FAMILIES:
        fam_lower = fam.lower()
        mappings = [
            ("annual_surface_rating", f"{fam_lower}_rating", np.nan),
            ("surface_rank", f"{fam_lower}_rank", np.nan),
            ("surface_comparison_evidence", f"{fam_lower}_comparison_evidence", 0),
            ("surface_top3_appearances", f"{fam_lower}_top3_appearances", 0),
        ]
        for metric, col, default in mappings:
            if (metric, fam) in wide.columns:
                base[col] = wide[(metric, fam)].reindex(base.index)
            else:
                base[col] = default
        base[f"{fam_lower}_comparison_evidence"] = base[f"{fam_lower}_comparison_evidence"].fillna(0).astype(int)
        base[f"{fam_lower}_top3_appearances"] = base[f"{fam_lower}_top3_appearances"].fillna(0).astype(int)

    out = base.reset_index()
    out = out.sort_values(
        ["year", "primary_strength_relative", "horse_pk"],
        ascending=[True, False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Streaming overlap accumulator.
# ---------------------------------------------------------------------------

def update_overlap_stats(acc: Dict[Tuple[str, str], dict], surface_df: pd.DataFrame):
    if surface_df.empty:
        return
    reliable = surface_df[
        (surface_df.surface_comparison_evidence >= core.RELIABLE_SURFACE_COMPARISON_EVIDENCE)
        & (surface_df.surface_top3_appearances >= core.RELIABLE_SURFACE_TOP3_APPEARANCES)
    ][["year", "horse_pk", "surface_family", "annual_surface_rating"]]

    fams = list(core.SURFACE_FAMILIES)
    for i, a in enumerate(fams):
        ga = reliable[reliable.surface_family == a][["year", "horse_pk", "annual_surface_rating"]]
        if ga.empty:
            continue
        ga = ga.rename(columns={"annual_surface_rating": "a"})
        for b in fams[i + 1:]:
            gb = reliable[reliable.surface_family == b][["year", "horse_pk", "annual_surface_rating"]]
            if gb.empty:
                continue
            gb = gb.rename(columns={"annual_surface_rating": "b"})
            m = ga.merge(gb, on=["year", "horse_pk"], how="inner")
            if m.empty:
                continue
            x = m["a"].to_numpy(float)
            y = m["b"].to_numpy(float)
            s = acc.setdefault((a, b), {
                "n": 0, "sx": 0.0, "sy": 0.0, "sx2": 0.0, "sy2": 0.0,
                "sxy": 0.0, "sabs": 0.0, "both110": 0, "both115": 0,
            })
            s["n"] += len(x)
            s["sx"] += float(x.sum()); s["sy"] += float(y.sum())
            s["sx2"] += float(np.dot(x, x)); s["sy2"] += float(np.dot(y, y))
            s["sxy"] += float(np.dot(x, y))
            s["sabs"] += float(np.abs(x - y).sum())
            s["both110"] += int(((x >= 110) & (y >= 110)).sum())
            s["both115"] += int(((x >= 115) & (y >= 115)).sum())


def overlap_frame(acc: Dict[Tuple[str, str], dict]) -> pd.DataFrame:
    rows = []
    for (a, b), s in sorted(acc.items()):
        n = s["n"]
        if n <= 0:
            continue
        num = n * s["sxy"] - s["sx"] * s["sy"]
        denx = n * s["sx2"] - s["sx"] ** 2
        deny = n * s["sy2"] - s["sy"] ** 2
        corr = num / math.sqrt(denx * deny) if n >= 3 and denx > 0 and deny > 0 else np.nan
        rows.append({
            "surface_a": a,
            "surface_b": b,
            "horse_years": n,
            "mean_rating_a": s["sx"] / n,
            "mean_rating_b": s["sy"] / n,
            "mean_abs_rating_gap": s["sabs"] / n,
            "pearson_same_horse_year": corr,
            "both_110_plus_share": s["both110"] / n,
            "both_115_plus_share": s["both115"] / n,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Optimized grade-free multi-surface hard-split rating")
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--year-start", type=int, default=None)
    ap.add_argument("--year-end", type=int, default=None)
    ap.add_argument("--years", default=None)
    ap.add_argument("--min-valid-year", type=int, default=DEFAULT_MIN_YEAR)
    ap.add_argument("--max-valid-year", type=int, default=DEFAULT_MAX_YEAR)
    ap.add_argument("--rebuild", action="store_true", help="Discard checkpoints and recompute all selected groups")
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

    sig = source_signature(races_path)
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
    atomic_write_text(json.dumps(sig, ensure_ascii=False, indent=2), manifest_path)

    print("=" * 78, flush=True)
    print(f"Grade-free Multi-Surface Hard Split {SCRIPT_VERSION}", flush=True)
    print(f"input : {races_path}", flush=True)
    print(f"output: {out_dir}", flush=True)
    print("mode  : STREAMING PARTS + RESUME CHECKPOINT", flush=True)
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

    exact_years = parse_years(args.years)
    if exact_years is not None:
        exact_years = {y for y in exact_years if valid_min <= y <= valid_max}
        df = df[df.year.isin(exact_years)].copy()
    else:
        if args.year_start is not None:
            df = df[df.year >= max(valid_min, int(args.year_start))].copy()
        if args.year_end is not None:
            df = df[df.year <= min(valid_max, int(args.year_end))].copy()

    filtered_rows = len(df)
    # Cheap classification here, then RaceObj construction only for one group at a time.
    df["_surface_family"] = df["surface"].map(core.surface_family)
    group_keys = sorted((int(y), str(f)) for y, f in df[["year", "_surface_family"]].drop_duplicates().itertuples(index=False, name=None))

    done_before = sum(completed_group(parts_root, y, f) for y, f in group_keys)
    print(f"groups: {len(group_keys)}  already completed: {done_before}", flush=True)

    for idx, (year, fam) in enumerate(group_keys, start=1):
        gp = group_part_paths(parts_root, year, fam)
        if gp["done"].exists():
            print(f"[{idx}/{len(group_keys)}] {year} {fam}: checkpoint -> SKIP", flush=True)
            continue

        gdf = df[(df.year == year) & (df._surface_family == fam)].drop(columns=["_surface_family"])
        race_objs, surface_counts, duplicate_pk_races = core.build_race_objects(gdf)
        print(f"[{idx}/{len(group_keys)}] {year} {fam}: races={len(race_objs)}", flush=True)

        rdf, meta = core.fit_year_surface(year, fam, race_objs)
        if not rdf.empty:
            rdf["surface_reliable"] = (
                (rdf.surface_comparison_evidence >= core.RELIABLE_SURFACE_COMPARISON_EVIDENCE)
                & (rdf.surface_top3_appearances >= core.RELIABLE_SURFACE_TOP3_APPEARANCES)
            ).astype(np.int8)
        dhorse, dsum = core.build_dynamic_outputs(year, fam, race_objs, rdf) if not rdf.empty else (pd.DataFrame(), pd.DataFrame())

        # Atomic part writes. DONE is written last; a crash before DONE simply recomputes this group.
        gp["dir"].mkdir(parents=True, exist_ok=True)
        if not rdf.empty:
            atomic_to_csv(rdf, gp["surface"])
        elif gp["surface"].exists():
            gp["surface"].unlink()
        if not dhorse.empty:
            atomic_to_csv(dhorse, gp["dynamic"])
        elif gp["dynamic"].exists():
            gp["dynamic"].unlink()
        if not dsum.empty:
            atomic_to_csv(dsum, gp["dynamic_summary"])
        elif gp["dynamic_summary"].exists():
            gp["dynamic_summary"].unlink()

        meta2 = dict(meta)
        meta2["duplicate_pk_races"] = int(duplicate_pk_races)
        atomic_to_csv(pd.DataFrame([meta2]), gp["meta"])
        atomic_write_text(f"{SCRIPT_VERSION}\n", gp["done"])

        # Release group-heavy objects promptly on Pyto/iPhone.
        del gdf, race_objs, rdf, dhorse, dsum

    # Raw source frame is no longer needed before finalization.
    del df

    # ------------------------------------------------------------------
    # Finalization: bounded-memory, with visible progress.
    # ------------------------------------------------------------------
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

    part_map = {(y, f): group_part_paths(parts_root, y, f) for y, f in group_keys}

    print("[final 1/5] assembling raw surface/dynamic CSVs (streaming)...", flush=True)
    concat_part_files([part_map[k]["surface"] for k in group_keys if part_map[k]["surface"].exists()], outputs["surface_horse"])
    concat_part_files([part_map[k]["dynamic"] for k in group_keys if part_map[k]["dynamic"].exists()], outputs["dynamic_horse"])
    concat_part_files([part_map[k]["dynamic_summary"] for k in group_keys if part_map[k]["dynamic_summary"].exists()], outputs["dynamic_summary"])
    concat_part_files([part_map[k]["meta"] for k in group_keys if part_map[k]["meta"].exists()], outputs["annual_meta"])
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
    years = sorted({y for y, _ in group_keys})

    for yi, year in enumerate(years, start=1):
        year_keys = [k for k in group_keys if k[0] == year]
        sframes = [read_if_exists(part_map[k]["surface"]) for k in year_keys if part_map[k]["surface"].exists()]
        sframes = [x for x in sframes if not x.empty]
        if not sframes:
            continue
        surf_year = pd.concat(sframes, ignore_index=True)

        mdf = fast_multisurface_summary(surf_year)
        first_multi = append_df(mdf, multi_tmp, first_multi)
        update_overlap_stats(overlap_acc, surf_year)

        dframes = [read_if_exists(part_map[k]["dynamic"]) for k in year_keys if part_map[k]["dynamic"].exists()]
        dframes = [x for x in dframes if not x.empty]
        if dframes:
            dyn_year = pd.concat(dframes, ignore_index=True)
            jdf = core.build_japan_surface_ranking(surf_year, dyn_year)
            first_japan = append_df(jdf, japan_tmp, first_japan)
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
    atomic_to_csv(overlap_frame(overlap_acc), outputs["overlap"])
    print("[final 3/5] done", flush=True)

    print("[final 4/5] writing report...", flush=True)
    completed = sum(completed_group(parts_root, y, f) for y, f in group_keys)
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
        "- year x surface結果を各group終了時にatomic保存",
        "- DONE checkpointで中断再開",
        "- source signature変更時は_partsを自動無効化",
        "- 全group DataFrameをメモリ保持しない",
        "- final raw CSVはpartを1個ずつ読み込んで連結",
        "- multi-surface集約は年単位vectorized処理",
        "- Japan順位も年単位",
        "- overlapは十分統計量だけ累積",
        "",
        "Rating semantics are unchanged from v1.0.1 hard-surface split.",
        "TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN are fitted separately.",
        "",
        "If interrupted, rerun the same command. Completed groups will be skipped.",
    ]
    atomic_write_text("\n".join(report) + "\n", outputs["report"])
    print("[final 4/5] done", flush=True)

    print("[final 5/5] complete", flush=True)
    print("Done.", flush=True)
    for key, path in outputs.items():
        if path.exists():
            print(f"  {key}: {path}", flush=True)


if __name__ == "__main__":
    main()
