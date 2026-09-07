#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fast-final launcher for grade_free_multisurface_dynamic_v1_0.

v1.0.2 changes only post-processing / input sanity handling:
- keeps the v1.0 surface-specific BT/context/dynamic-network calculation intact;
- keeps v1.0.1 surface normalization (TURF/DIRT/SYNTHETIC/OTHER/UNKNOWN);
- replaces the very slow Python per-horse-year multisurface summary loop with
  vectorized pandas operations;
- ignores impossible future race years (> current calendar year) and years <1700;
- prints progress messages for the heavy final aggregation stages.

The rating model itself is unchanged from v1.0 + v1.0.1 surface classifier.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

import numpy as np
import pandas as pd

import grade_free_multisurface_dynamic_v1_0 as core


# ---------------------------------------------------------------------------
# v1.0.1 surface normalization
# ---------------------------------------------------------------------------
def surface_family_v102(value) -> str:
    raw = core._norm(value)
    compact = re.sub(r"[^A-Z0-9一-龠ぁ-んァ-ヶ]+", " ", raw).strip()
    compact_no_space = compact.replace(" ", "")

    if not compact or compact in {"UNKNOWN", "UNK", "N A", "NA", "?", "NONE"}:
        return "UNKNOWN"

    synth_phrases = (
        "SYNTHETIC", "SYNTH", "ALL WEATHER", "ALLWEATHER", "POLYTRACK",
        "TAPETA", "CUSHION TRACK", "CUSHION", "FIBRESAND", "FIBERSAND",
        "PRO RIDE", "PRORIDE", "VISCO RIDE", "ARTIFICIAL",
    )
    if any(token in compact for token in synth_phrases):
        return "SYNTHETIC"
    if compact_no_space in {"AW", "AWT"}:
        return "SYNTHETIC"

    if "TURF" in compact or "GRASS" in compact or "芝" in raw:
        return "TURF"
    if "DIRT" in compact or "SAND" in compact or "ダート" in raw:
        return "DIRT"
    return "OTHER"


# ---------------------------------------------------------------------------
# Sanity filter for obviously broken race years
# ---------------------------------------------------------------------------
_original_build_race_objects = core.build_race_objects


def build_race_objects_sane(df: pd.DataFrame):
    current_year = datetime.now().year
    before = len(df)
    y = pd.to_numeric(df["year"], errors="coerce")
    good = y.between(1700, current_year, inclusive="both")
    bad = df.loc[~good, ["year", "race_id", "race_name"]].copy() if (~good).any() else pd.DataFrame()
    if len(bad):
        print(f"[sanity] ignore invalid race-year rows: {len(bad):,} (valid 1700-{current_year})", flush=True)
        print("[sanity] invalid years: " + ", ".join(map(str, sorted(pd.to_numeric(bad['year'], errors='coerce').dropna().astype(int).unique())[:20])), flush=True)
    out = _original_build_race_objects(df.loc[good].copy())
    if before and len(df.loc[good]) != before:
        print(f"[sanity] rows kept: {len(df.loc[good]):,}/{before:,}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Vectorized multisurface summary -- same semantics as v1.0
# ---------------------------------------------------------------------------
def build_multisurface_summary_fast(surface_df: pd.DataFrame) -> pd.DataFrame:
    print("[final 1/4] building multi-surface horse summary (vectorized)...", flush=True)
    if surface_df.empty:
        return pd.DataFrame()

    df = surface_df.copy()
    keys = ["year", "horse_pk"]
    rel = (
        (df["surface_comparison_evidence"] >= core.RELIABLE_SURFACE_COMPARISON_EVIDENCE) &
        (df["surface_top3_appearances"] >= core.RELIABLE_SURFACE_TOP3_APPEARANCES)
    )
    df["_reliable"] = rel.astype(np.int8)

    # Horse name: first non-empty value per year/horse. Rows normally already agree.
    names = (
        df.assign(_name=df["horse_name"].fillna("").astype(str))
          .sort_values(keys + ["_name"], ascending=[True, True, False])
          .drop_duplicates(keys)[keys + ["_name"]]
          .rename(columns={"_name": "horse_name"})
    )

    # If a horse-year has any reliable surface, Primary Strength is max among
    # reliable surfaces; otherwise max among all available surfaces.
    has_rel = df.groupby(keys, sort=False)["_reliable"].transform("max").astype(bool)
    eligible = df[(df["_reliable"].astype(bool)) | (~has_rel)].copy()
    primary = (
        eligible.sort_values(keys + ["annual_surface_rating", "surface_family"],
                             ascending=[True, True, False, True])
                .drop_duplicates(keys)
                [keys + ["surface_family", "annual_surface_rating",
                         "surface_comparison_evidence", "surface_top3_appearances"]]
                .rename(columns={
                    "surface_family": "primary_surface_family",
                    "annual_surface_rating": "primary_strength_relative",
                    "surface_comparison_evidence": "primary_surface_comparison_evidence",
                    "surface_top3_appearances": "primary_surface_top3_appearances",
                })
    )

    counts = df.groupby(keys, sort=False).agg(
        surface_rating_count=("surface_family", "size"),
        reliable_surface_count=("_reliable", "sum"),
        total_surface_top3_appearances=("surface_top3_appearances", "sum"),
    ).reset_index()
    counts["surface_rating_count"] = counts["surface_rating_count"].astype(int)
    counts["reliable_surface_count"] = counts["reliable_surface_count"].astype(int)
    counts["multi_surface_verified"] = (counts["reliable_surface_count"] >= 2).astype(int)
    counts["total_surface_top3_appearances"] = counts["total_surface_top3_appearances"].astype(int)

    # Floor = min reliable surface rating when >=2 reliable surfaces.
    rel_df = df[df["_reliable"] == 1]
    floor = rel_df.groupby(keys, sort=False)["annual_surface_rating"].min().rename("multi_surface_floor_relative").reset_index()
    counts = counts.merge(floor, on=keys, how="left")
    counts.loc[counts["reliable_surface_count"] < 2, "multi_surface_floor_relative"] = np.nan

    # Effective number of surfaces = exp(entropy of Top3 appearance shares).
    top3 = pd.to_numeric(df["surface_top3_appearances"], errors="coerce").fillna(0).astype(float)
    totals = top3.groupby([df["year"], df["horse_pk"]]).transform("sum")
    p = np.divide(top3, totals, out=np.zeros(len(df), dtype=float), where=(totals.to_numpy() > 0))
    entropy_term = np.zeros(len(df), dtype=float)
    mask = p > 0
    entropy_term[mask] = -p[mask] * np.log(p[mask])
    tmp_entropy = df[keys].copy()
    tmp_entropy["_h"] = entropy_term
    entropy = tmp_entropy.groupby(keys, sort=False)["_h"].sum().reset_index()
    entropy["effective_top3_surface_count"] = np.exp(entropy["_h"])
    entropy = entropy.drop(columns="_h")

    out = names.merge(primary, on=keys, how="inner").merge(counts, on=keys, how="left").merge(entropy, on=keys, how="left")
    out["primary_minus_floor"] = out["primary_strength_relative"] - out["multi_surface_floor_relative"]

    # Wide per-family columns.
    base = df[keys + ["surface_family", "annual_surface_rating", "surface_rank",
                      "surface_comparison_evidence", "surface_top3_appearances"]].copy()
    for fam in core.SURFACE_FAMILIES:
        g = base[base["surface_family"] == fam].drop_duplicates(keys)
        lname = fam.lower()
        g = g[keys + ["annual_surface_rating", "surface_rank",
                      "surface_comparison_evidence", "surface_top3_appearances"]].rename(columns={
            "annual_surface_rating": f"{lname}_rating",
            "surface_rank": f"{lname}_rank",
            "surface_comparison_evidence": f"{lname}_comparison_evidence",
            "surface_top3_appearances": f"{lname}_top3_appearances",
        })
        out = out.merge(g, on=keys, how="left")
        out[f"{lname}_comparison_evidence"] = out[f"{lname}_comparison_evidence"].fillna(0).astype(int)
        out[f"{lname}_top3_appearances"] = out[f"{lname}_top3_appearances"].fillna(0).astype(int)

    out = out.sort_values(["year", "primary_strength_relative", "horse_pk"],
                          ascending=[True, False, True]).reset_index(drop=True)
    print(f"[final 1/4] done: {len(out):,} horse-years", flush=True)
    return out


_original_overlap = core.build_surface_overlap_matrix
_original_japan = core.build_japan_surface_ranking


def overlap_progress(surface_df):
    print("[final 2/4] building cross-surface overlap matrix...", flush=True)
    out = _original_overlap(surface_df)
    print(f"[final 2/4] done: {len(out):,} surface-pairs", flush=True)
    return out


def japan_progress(surface_df, dynamic_df):
    print("[final 3/4] building Japan surface rankings...", flush=True)
    out = _original_japan(surface_df, dynamic_df)
    print(f"[final 3/4] done: {len(out):,} rows", flush=True)
    print("[final 4/4] writing CSV files...", flush=True)
    return out


core.surface_family = surface_family_v102
core.build_race_objects = build_race_objects_sane
core.build_multisurface_summary = build_multisurface_summary_fast
core.build_surface_overlap_matrix = overlap_progress
core.build_japan_surface_ranking = japan_progress
core.SCRIPT_VERSION = "1.0.2-multisurface-hard-split-fastfinal"


if __name__ == "__main__":
    core.main()
