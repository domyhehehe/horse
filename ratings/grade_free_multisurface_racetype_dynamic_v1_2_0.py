#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_racetype_dynamic_v1_2_0.py
=================================================

v1.2 hard-split rating launcher.

Change from v1.0.4/v1.1.x pipeline:
    year x RACE_TYPE x SURFACE_FAMILY

RACE_TYPE:
    FLAT / JUMP

SURFACE_FAMILY:
    TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN

The race-type split happens BEFORE Bradley-Terry/context fitting and BEFORE
dynamic-network partitioning. Therefore flat turf and jump turf can never
borrow comparison/context/network support from each other.

Important:
- grade and series_grade are NOT read or used.
- Jump classification uses only race_name / comment / condition.
- The Jump regex is the same established project rule used by the existing
  stakes-search/GBS utilities, minus grade/series_grade fields so this remains
  grade-free.
- Rating semantics inside each year x race_type x surface group are unchanged
  from the v1.0.1 hard-surface core.
- Streaming atomic part files + DONE checkpoints are retained.
- Existing v1.0.4 checkpoints are NOT compatible because the grouping changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

import grade_free_multisurface_dynamic_v1_0_3_stream_checkpoint as opt

core = opt.core

SCRIPT_VERSION = "1.2.0-flat-jump-hard-split"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = "grade_free_multisurface_results_v1_2_0_flat_jump"
DEFAULT_MIN_YEAR = 1700
DEFAULT_MAX_YEAR = datetime.now().year

RACE_TYPES = ("FLAT", "JUMP")

JUMP_PATTERN = re.compile(
    r"(?:\bHURDLES?\b|\bJUMPS?\b|\bSTEEPLE(?:CHASE)?S?\b|\bNOVICES?\s+CHASE\b|"
    r"\bHANDICAP\s+CHASE\b|\bCHAMPION\s+CHASE\b|\bCELEBRATION\s+CHASE\b|"
    r"\bCHASE\b|\bNATIONAL\s+HUNT\b|\bNH\s+FLAT\b|\bPOINT\s+TO\s+POINT\b|"
    r"\bCROSS\s+COUNTRY\b|障害|ハードル|スティープルチェイス)",
    re.IGNORECASE,
)

# Grade-free: grade / series_grade are intentionally absent.
EXTRA_RACE_TYPE_COLS = ["comment", "condition"]
RACE_USECOLS = list(dict.fromkeys(list(core.RACE_USECOLS) + EXTRA_RACE_TYPE_COLS))


def classify_race_type_frame(df: pd.DataFrame) -> pd.Series:
    """Vectorized Flat/Jump classification without reading grade fields."""
    text = pd.Series("", index=df.index, dtype="object")
    for col in ("race_name", "comment", "condition"):
        if col in df.columns:
            text = text.str.cat(df[col].fillna("").astype(str), sep=" ")
    is_jump = text.str.contains(JUMP_PATTERN, na=False)
    return pd.Series(np.where(is_jump, "JUMP", "FLAT"), index=df.index)


def local_signature(path: Path) -> dict:
    sig = opt.source_signature(path)
    sig["script_version"] = SCRIPT_VERSION
    sig["grouping"] = "year x race_type x surface_family"
    sig["race_type_classifier"] = (
        "race_name+comment+condition / established JUMP_PATTERN / no grade fields"
    )
    return sig


def part_dir(parts_root: Path, year: int, race_type: str, family: str) -> Path:
    return parts_root / f"{int(year):04d}" / str(race_type).upper() / str(family).upper()


def group_part_paths(parts_root: Path, year: int, race_type: str, family: str) -> dict:
    d = part_dir(parts_root, year, race_type, family)
    return {
        "dir": d,
        "surface": d / "surface_horse.csv",
        "dynamic": d / "dynamic_horse.csv",
        "dynamic_summary": d / "dynamic_summary.csv",
        "meta": d / "annual_meta.csv",
        "done": d / "DONE",
    }


def completed_group(parts_root: Path, year: int, race_type: str, family: str) -> bool:
    return group_part_paths(parts_root, year, race_type, family)["done"].exists()


def add_race_type(df: pd.DataFrame, race_type: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if "race_type" in out.columns:
        out["race_type"] = race_type
    else:
        pos = 1 if "year" in out.columns else 0
        out.insert(pos, "race_type", race_type)
    return out


def concat_part_files_schema_safe(paths: Iterable[Path], output: Path):
    """Memory-bounded union-schema concatenation."""
    paths = [Path(p) for p in paths if Path(p).exists() and Path(p).stat().st_size > 0]
    tmp = output.with_name(output.name + ".tmp")
    if tmp.exists():
        tmp.unlink()

    if not paths:
        pd.DataFrame().to_csv(tmp, index=False, encoding="utf-8-sig")
        tmp.replace(output)
        return

    union_cols: List[str] = []
    seen = set()
    readable: List[Path] = []
    for p in paths:
        try:
            cols = list(pd.read_csv(p, nrows=0).columns)
        except (pd.errors.EmptyDataError, UnicodeDecodeError):
            continue
        readable.append(p)
        for c in cols:
            if c not in seen:
                seen.add(c)
                union_cols.append(c)

    first = True
    for p in readable:
        try:
            x = pd.read_csv(p, low_memory=False)
        except pd.errors.EmptyDataError:
            continue
        if x.empty:
            continue
        x = x.reindex(columns=union_cols)
        x.to_csv(
            tmp,
            mode="w" if first else "a",
            header=first,
            index=False,
            encoding="utf-8-sig" if first else "utf-8",
        )
        first = False

    if first:
        pd.DataFrame(columns=union_cols).to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(output)


def build_japan_racetype_surface_ranking(dynamic_df: pd.DataFrame) -> pd.DataFrame:
    """Japan-host Top3 view, ranked separately by race type and surface."""
    if dynamic_df is None or dynamic_df.empty:
        return pd.DataFrame()
    j = dynamic_df.copy()
    if "japan_top3_appearances" not in j.columns:
        return pd.DataFrame()
    j = j[pd.to_numeric(j["japan_top3_appearances"], errors="coerce").fillna(0) > 0].copy()
    if j.empty:
        return j
    j["japan_racetype_surface_rank"] = (
        j.groupby(["year", "race_type", "surface_family"])["annual_surface_rating"]
        .rank(method="min", ascending=False)
        .astype(int)
    )
    return j.sort_values(
        ["year", "race_type", "surface_family", "japan_racetype_surface_rank", "horse_pk"],
        kind="mergesort",
    ).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(
        description="Grade-free Flat/Jump x multi-surface hard-split dynamic rating"
    )
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
    out_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else (base_dir / DEFAULT_OUTPUT_DIR)
    )
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
        print("Checkpoint invalidated: source/script/grouping changed or --rebuild specified.", flush=True)
        if parts_root.exists():
            shutil.rmtree(parts_root)
    parts_root.mkdir(parents=True, exist_ok=True)
    opt.atomic_write_text(json.dumps(sig, ensure_ascii=False, indent=2), manifest_path)

    print("=" * 78, flush=True)
    print(f"Grade-free Flat/Jump Multi-Surface {SCRIPT_VERSION}", flush=True)
    print(f"input : {races_path}", flush=True)
    print(f"output: {out_dir}", flush=True)
    print("split : YEAR x RACE_TYPE x SURFACE_FAMILY", flush=True)
    print("grade / series_grade: NOT READ", flush=True)
    print("checkpoint: per year x race_type x surface", flush=True)
    print("=" * 78, flush=True)

    df = pd.read_csv(races_path, usecols=RACE_USECOLS, dtype=str, low_memory=False)
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
    print("Classifying FLAT/JUMP from race_name/comment/condition...", flush=True)
    df["_race_type"] = classify_race_type_frame(df)
    df["_surface_family"] = df["surface"].map(core.surface_family)

    type_counts = df["_race_type"].value_counts().to_dict()
    print(f"race-type rows: {type_counts}", flush=True)

    grouped = df.groupby(
        ["year", "_race_type", "_surface_family"],
        sort=True,
        observed=True,
    )
    group_keys = [
        (int(y), str(rt), str(fam))
        for y, rt, fam in grouped.groups.keys()
    ]
    group_keys.sort()

    done_before = sum(completed_group(parts_root, y, rt, fam) for y, rt, fam in group_keys)
    print(f"groups: {len(group_keys)}  already completed: {done_before}", flush=True)

    for idx, (year, race_type, fam) in enumerate(group_keys, start=1):
        gp = group_part_paths(parts_root, year, race_type, fam)
        if gp["done"].exists():
            print(
                f"[{idx}/{len(group_keys)}] {year} {race_type} {fam}: checkpoint -> SKIP",
                flush=True,
            )
            continue

        gdf = grouped.get_group((year, race_type, fam)).drop(
            columns=["_race_type", "_surface_family"]
        )
        race_objs, _surface_counts, duplicate_pk_races = core.build_race_objects(gdf)
        print(
            f"[{idx}/{len(group_keys)}] {year} {race_type} {fam}: races={len(race_objs)}",
            flush=True,
        )

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

        rdf = add_race_type(rdf, race_type)
        dhorse = add_race_type(dhorse, race_type)
        dsum = add_race_type(dsum, race_type)

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
        meta2["race_type"] = race_type
        meta2["duplicate_pk_races"] = int(duplicate_pk_races)
        meta_df = pd.DataFrame([meta2])
        if "year" in meta_df.columns and "race_type" in meta_df.columns:
            cols = list(meta_df.columns)
            cols.remove("race_type")
            year_pos = cols.index("year") + 1 if "year" in cols else 0
            cols.insert(year_pos, "race_type")
            meta_df = meta_df[cols]
        opt.atomic_to_csv(meta_df, gp["meta"])
        opt.atomic_write_text(f"{SCRIPT_VERSION}\n", gp["done"])
        print(f"    saved checkpoint: {year} {race_type} {fam}", flush=True)

        del gdf, race_objs, rdf, dhorse, dsum

    del grouped
    del df

    outputs = {
        "surface_horse": out_dir / "grade_free_racetype_surface_horse_ratings.csv",
        "dynamic_horse": out_dir / "grade_free_racetype_surface_dynamic_network_ratings.csv",
        "dynamic_summary": out_dir / "grade_free_racetype_surface_dynamic_network_summary.csv",
        "multi": out_dir / "grade_free_racetype_horse_multisurface_summary.csv",
        "overlap": out_dir / "grade_free_racetype_surface_overlap_matrix.csv",
        "japan": out_dir / "grade_free_japan_racetype_surface_ranking.csv",
        "annual_meta": out_dir / "grade_free_racetype_surface_annual_summary.csv",
        "report": out_dir / "grade_free_racetype_multisurface_report.txt",
    }
    part_map = {
        (y, rt, fam): group_part_paths(parts_root, y, rt, fam)
        for y, rt, fam in group_keys
    }

    print("[final 1/5] assembling raw CSVs (schema-safe, one part at a time)...", flush=True)
    concat_part_files_schema_safe(
        [part_map[k]["surface"] for k in group_keys if part_map[k]["surface"].exists()],
        outputs["surface_horse"],
    )
    concat_part_files_schema_safe(
        [part_map[k]["dynamic"] for k in group_keys if part_map[k]["dynamic"].exists()],
        outputs["dynamic_horse"],
    )
    concat_part_files_schema_safe(
        [
            part_map[k]["dynamic_summary"]
            for k in group_keys
            if part_map[k]["dynamic_summary"].exists()
        ],
        outputs["dynamic_summary"],
    )
    concat_part_files_schema_safe(
        [part_map[k]["meta"] for k in group_keys if part_map[k]["meta"].exists()],
        outputs["annual_meta"],
    )
    print("[final 1/5] done", flush=True)

    print("[final 2/5] building race-type-aware summaries year-by-year...", flush=True)
    multi_tmp = outputs["multi"].with_name(outputs["multi"].name + ".tmp")
    japan_tmp = outputs["japan"].with_name(outputs["japan"].name + ".tmp")
    for p in (multi_tmp, japan_tmp):
        if p.exists():
            p.unlink()

    first_multi = True
    first_japan = True
    overlap_by_type: Dict[str, dict] = defaultdict(dict)

    by_year: Dict[int, List[Tuple[int, str, str]]] = defaultdict(list)
    for k in group_keys:
        by_year[k[0]].append(k)
    years = sorted(by_year)

    for yi, year in enumerate(years, start=1):
        year_keys = by_year[year]
        sframes = [
            opt.read_if_exists(part_map[k]["surface"])
            for k in year_keys
            if part_map[k]["surface"].exists()
        ]
        sframes = [x for x in sframes if not x.empty]
        if not sframes:
            continue
        surf_year = pd.concat(sframes, ignore_index=True)

        dframes = [
            opt.read_if_exists(part_map[k]["dynamic"])
            for k in year_keys
            if part_map[k]["dynamic"].exists()
        ]
        dframes = [x for x in dframes if not x.empty]
        dyn_year = pd.concat(dframes, ignore_index=True) if dframes else pd.DataFrame()

        for race_type, surf_rt in surf_year.groupby("race_type", sort=True):
            mdf = opt.fast_multisurface_summary(surf_rt)
            if not mdf.empty:
                mdf.insert(1, "race_type", race_type)
                first_multi = opt.append_df(mdf, multi_tmp, first_multi)

            opt.update_overlap_stats(overlap_by_type[str(race_type)], surf_rt)

            if not dyn_year.empty:
                dyn_rt = dyn_year[dyn_year["race_type"] == race_type].copy()
                jdf = build_japan_racetype_surface_ranking(dyn_rt)
                if not jdf.empty:
                    first_japan = opt.append_df(jdf, japan_tmp, first_japan)
                del dyn_rt, jdf
            del mdf

        del surf_year, dyn_year, sframes, dframes
        if yi == 1 or yi == len(years) or yi % 25 == 0:
            print(f"  summary years {yi}/{len(years)}", flush=True)

    if first_multi:
        pd.DataFrame().to_csv(multi_tmp, index=False, encoding="utf-8-sig")
    if first_japan:
        pd.DataFrame().to_csv(japan_tmp, index=False, encoding="utf-8-sig")
    multi_tmp.replace(outputs["multi"])
    japan_tmp.replace(outputs["japan"])
    print("[final 2/5] done", flush=True)

    print("[final 3/5] writing Flat/Jump-specific cross-surface overlap...", flush=True)
    overlap_frames = []
    for rt in RACE_TYPES:
        frame = opt.overlap_frame(overlap_by_type.get(rt, {}))
        if not frame.empty:
            frame.insert(0, "race_type", rt)
            overlap_frames.append(frame)
    overlap_df = (
        pd.concat(overlap_frames, ignore_index=True)
        if overlap_frames
        else pd.DataFrame()
    )
    opt.atomic_to_csv(overlap_df, outputs["overlap"])
    print("[final 3/5] done", flush=True)

    print("[final 4/5] writing report...", flush=True)
    completed = sum(completed_group(parts_root, y, rt, fam) for y, rt, fam in group_keys)
    group_type_counts = Counter(rt for _, rt, _ in group_keys)
    report = [
        "Grade-free Flat/Jump x Multi-Surface Hard Split",
        f"script: {SCRIPT_VERSION}",
        f"core: {getattr(core, 'SCRIPT_VERSION', 'unknown')}",
        f"input: {races_path}",
        f"source rows: {source_rows:,}",
        f"selected rows: {filtered_rows:,}",
        f"non-numeric year rows ignored: {bad_non_numeric:,}",
        f"anomalous years ignored: {bad_years}",
        f"race-type row counts: {type_counts}",
        f"groups: {len(group_keys):,}",
        f"group counts by race type: {dict(group_type_counts)}",
        f"completed groups: {completed:,}",
        "",
        "Hard split:",
        "  YEAR x RACE_TYPE x SURFACE_FAMILY",
        "  RACE_TYPE = FLAT / JUMP",
        "  SURFACE_FAMILY = TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN",
        "",
        "Race-type classifier:",
        "  fields read: race_name / comment / condition",
        "  grade / series_grade: NOT READ",
        "  jump terms include HURDLE, JUMP, STEEPLECHASE, CHASE, NATIONAL HUNT,",
        "  POINT TO POINT, CROSS COUNTRY, 障害, ハードル, スティープルチェイス.",
        "",
        "Semantics:",
        "  Rating logic inside each group is unchanged from v1.0.1 surface core.",
        "  Flat Turf and Jump Turf never share BT/context/dynamic-network evidence.",
        "  Breadth coefficients are not changed by this script.",
        "",
        "Checkpoint:",
        "  Each year x race_type x surface group is atomically saved.",
        "  Re-run the same script after interruption; completed groups are skipped.",
        "  v1.0.4 checkpoints are intentionally NOT reused because grouping changed.",
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
