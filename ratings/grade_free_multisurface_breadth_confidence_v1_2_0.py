#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_breadth_confidence_v1_2_0.py
===================================================

Breadth-confidence post-processing for the v1.2 Flat/Jump hard-split rating.

This keeps the v1.1.1 realized-breadth coefficients unchanged and only changes
identity/grouping keys from:
    year x surface_family
into:
    year x race_type x surface_family

Therefore Flat Turf and Jump Turf remain separated all the way through breadth
measurement, network inheritance, calibrated ranking and Japan views.

Grade-free rule:
- grade / series_grade are not read or used.
- race type is classified from race_name / comment / condition using the same
  established Jump pattern as the v1.2 rating launcher.

No JRA awards or result labels are used to tune coefficients.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

import grade_free_multisurface_breadth_confidence_v1_1_0 as base
import grade_free_multisurface_racetype_dynamic_v1_2_0 as split

SCRIPT_VERSION = "1.2.0-flat-jump-realized-breadth"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = "grade_free_multisurface_results_v1_2_0_flat_jump"

# v1.1.1 coefficients: intentionally unchanged.
RETENTION_FLOOR = 0.25
INTERNAL_WEIGHT = 0.35
INHERITED_NETWORK_WEIGHT = 0.15
HORSE_VENUE_WEIGHT = 0.15
HORSE_BRIDGE_WEIGHT = 0.20
HORSE_INTERNATIONAL_WEIGHT = 0.05

NETWORK_INHERITANCE_FLOOR = 0.25
NETWORK_INHERITANCE_REALIZED_WEIGHT = 0.75
REALIZED_VENUE_WEIGHT = 0.65
REALIZED_OPPONENT_WEIGHT = 0.35

RACE_USECOLS = [
    "race_id", "year", "race_name", "country", "track", "surface",
    "comment", "condition", "winner_pk", "second_pk", "third_pk",
]


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _safe_ratio(num: pd.Series, den: pd.Series, when_zero: float = 0.0) -> pd.Series:
    num = _num(num)
    den = _num(den)
    out = pd.Series(np.full(len(num), float(when_zero)), index=num.index, dtype=float)
    mask = den > 1e-12
    out.loc[mask] = num.loc[mask] / den.loc[mask]
    return out.clip(0.0, 1.0)


def discover_results_dir(base_dir: Path, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    preferred = base_dir / DEFAULT_RESULTS_DIR
    if (preferred / "grade_free_racetype_surface_dynamic_network_ratings.csv").exists():
        return preferred
    if (base_dir / "grade_free_racetype_surface_dynamic_network_ratings.csv").exists():
        return base_dir
    candidates = []
    for p in base_dir.glob("grade_free_multisurface_results_v1_2*"):
        if p.is_dir() and (p / "grade_free_racetype_surface_dynamic_network_ratings.csv").exists():
            candidates.append(p)
    if not candidates:
        raise FileNotFoundError("Could not find v1.2 Flat/Jump multi-surface result directory")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def read_required_results(results_dir: Path):
    dyn_path = results_dir / "grade_free_racetype_surface_dynamic_network_ratings.csv"
    sum_path = results_dir / "grade_free_racetype_surface_dynamic_network_summary.csv"
    if not dyn_path.exists() or not sum_path.exists():
        raise FileNotFoundError("v1.2 race-type dynamic rating/summary CSV missing")

    dyn_cols = [
        "year", "race_type", "surface_family", "horse_pk", "horse_name",
        "annual_surface_rating", "dynamic_network_id",
        "dynamic_network_horses", "dynamic_network_races",
        "japan_top3_appearances",
    ]
    sum_cols = [
        "year", "race_type", "surface_family", "dynamic_network_id",
        "dynamic_network_horses", "dynamic_network_races",
        "internal_comparisons", "external_comparisons",
        "bridge_horses", "conductance", "dominant_country",
    ]
    dyn = pd.read_csv(dyn_path, usecols=dyn_cols, low_memory=False)
    net = pd.read_csv(sum_path, usecols=sum_cols, low_memory=False)

    dyn["horse_pk"] = dyn["horse_pk"].map(base.normalize_pk)
    dyn["race_type"] = dyn["race_type"].astype(str).str.upper().str.strip()
    net["race_type"] = net["race_type"].astype(str).str.upper().str.strip()
    dyn["year"] = pd.to_numeric(dyn["year"], errors="coerce").astype("Int64")
    net["year"] = pd.to_numeric(net["year"], errors="coerce").astype("Int64")
    dyn = dyn[dyn.year.notna()].copy()
    net = net[net.year.notna()].copy()
    dyn["year"] = dyn["year"].astype(int)
    net["year"] = net["year"].astype(int)
    dyn["dynamic_network_id"] = pd.to_numeric(
        dyn["dynamic_network_id"], errors="coerce"
    ).astype("Int64")
    net["dynamic_network_id"] = pd.to_numeric(
        net["dynamic_network_id"], errors="coerce"
    ).astype("Int64")
    return dyn, net


def build_mapping(dyn: pd.DataFrame) -> Dict[Tuple[int, str, str, str], int]:
    out = {}
    cols = ["year", "race_type", "surface_family", "horse_pk", "dynamic_network_id"]
    for r in dyn[cols].itertuples(index=False):
        if pd.isna(r.dynamic_network_id):
            continue
        out[(
            int(r.year), str(r.race_type), str(r.surface_family),
            base.normalize_pk(r.horse_pk),
        )] = int(r.dynamic_network_id)
    return out


def load_races(races_path: Path, min_year: int, max_year: int) -> pd.DataFrame:
    df = pd.read_csv(races_path, usecols=RACE_USECOLS, dtype=str, low_memory=False)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df[df.year.notna()].copy()
    df["year"] = df["year"].astype(int)
    df = df[(df.year >= min_year) & (df.year <= max_year)].copy()
    df["race_type"] = split.classify_race_type_frame(df)
    df["surface_family_norm"] = df["surface"].map(split.core.surface_family)
    df["country_norm"] = df["country"].map(base.normalize_country)
    df["track_norm"] = df["track"].map(base.normalize_track)
    df["domain_norm"] = [
        base.domain_name(c, t)
        for c, t in zip(df["country_norm"].astype(str), df["track_norm"].astype(str))
    ]
    return df


def build_breadth_tables(
    races: pd.DataFrame,
    mapping: Dict[Tuple[int, str, str, str], int],
):
    net_top3_domain = defaultdict(Counter)
    net_win_domain = defaultdict(Counter)
    net_top3_country = defaultdict(Counter)
    net_win_country = defaultdict(Counter)
    net_opponents = defaultdict(Counter)

    horse_top3_domain = defaultdict(Counter)
    horse_win_domain = defaultdict(Counter)
    horse_top3_country = defaultdict(Counter)
    horse_win_country = defaultdict(Counter)
    horse_opponents = defaultdict(Counter)

    mapped_races = 0

    for row in races.itertuples(index=False):
        year = int(row.year)
        race_type = str(row.race_type)
        fam = str(row.surface_family_norm)
        country = str(row.country_norm)
        domain = str(row.domain_norm)

        pks = [
            base.normalize_pk(row.winner_pk),
            base.normalize_pk(row.second_pk),
            base.normalize_pk(row.third_pk),
        ]
        entries = []
        for placing, pk in enumerate(pks, start=1):
            if not pk:
                continue
            nid = mapping.get((year, race_type, fam, pk))
            if nid is None:
                continue
            entries.append((placing, pk, nid))
        if not entries:
            continue
        mapped_races += 1

        networks = sorted({nid for _, _, nid in entries})
        for nid in networks:
            nk = (year, race_type, fam, nid)
            net_top3_domain[nk][domain] += 1
            net_top3_country[nk][country] += 1

        winner_entries = [e for e in entries if e[0] == 1]
        if winner_entries:
            _placing, _winner_pk, winner_nid = winner_entries[0]
            nk = (year, race_type, fam, winner_nid)
            net_win_domain[nk][domain] += 1
            net_win_country[nk][country] += 1

        if len(networks) >= 2:
            for a in networks:
                ak = (year, race_type, fam, a)
                for b in networks:
                    if b != a:
                        net_opponents[ak][b] += 1

        for placing, pk, nid in entries:
            hk = (year, race_type, fam, pk)
            horse_top3_domain[hk][domain] += 1
            horse_top3_country[hk][country] += 1
            if placing == 1:
                horse_win_domain[hk][domain] += 1
                horse_win_country[hk][country] += 1
            for other_nid in networks:
                if other_nid != nid:
                    horse_opponents[hk][other_nid] += 1

    network_keys = set(net_top3_domain) | set(net_win_domain) | set(net_opponents)
    network_rows = []
    for nk in sorted(network_keys):
        year, race_type, fam, nid = nk
        td = net_top3_domain[nk]
        wd = net_win_domain[nk]
        tc = net_top3_country[nk]
        wc = net_win_country[nk]
        op = net_opponents[nk]
        network_rows.append({
            "year": year,
            "race_type": race_type,
            "surface_family": fam,
            "dynamic_network_id": nid,
            "top3_domain_count": len(td),
            "win_domain_count": len(wd),
            "effective_top3_domains": base.effective_count(td),
            "effective_win_domains": base.effective_count(wd),
            "top3_country_count": len(tc),
            "win_country_count": len(wc),
            "effective_top3_countries": base.effective_count(tc),
            "effective_win_countries": base.effective_count(wc),
            "opponent_network_count": len(op),
            "effective_opponent_networks": base.effective_count(op),
            "opponent_network_encounters": int(sum(op.values())),
            "top3_domains": " | ".join(f"{k}:{v}" for k, v in td.most_common(20)),
            "win_domains": " | ".join(f"{k}:{v}" for k, v in wd.most_common(20)),
        })

    horse_keys = (
        set(horse_top3_domain) | set(horse_win_domain) | set(horse_opponents)
    )
    horse_rows = []
    for hk in sorted(horse_keys):
        year, race_type, fam, pk = hk
        td = horse_top3_domain[hk]
        wd = horse_win_domain[hk]
        tc = horse_top3_country[hk]
        wc = horse_win_country[hk]
        op = horse_opponents[hk]
        horse_rows.append({
            "year": year,
            "race_type": race_type,
            "surface_family": fam,
            "horse_pk": pk,
            "horse_top3_domain_count": len(td),
            "horse_win_domain_count": len(wd),
            "horse_effective_top3_domains": base.effective_count(td),
            "horse_effective_win_domains": base.effective_count(wd),
            "horse_top3_country_count": len(tc),
            "horse_win_country_count": len(wc),
            "horse_effective_top3_countries": base.effective_count(tc),
            "horse_effective_win_countries": base.effective_count(wc),
            "horse_opponent_network_count": len(op),
            "horse_effective_opponent_networks": base.effective_count(op),
            "horse_opponent_network_encounters": int(sum(op.values())),
            "horse_top3_domains": " | ".join(f"{k}:{v}" for k, v in td.most_common(10)),
            "horse_win_domains": " | ".join(f"{k}:{v}" for k, v in wd.most_common(10)),
        })

    return pd.DataFrame(network_rows), pd.DataFrame(horse_rows), mapped_races


def calculate_ratings_realized(
    dyn: pd.DataFrame,
    netc: pd.DataFrame,
    horsec: pd.DataFrame,
) -> pd.DataFrame:
    keys_net = ["year", "race_type", "surface_family", "dynamic_network_id"]
    keys_horse = ["year", "race_type", "surface_family", "horse_pk"]

    net_cols = keys_net + [
        "internal_evidence_confidence",
        "venue_breadth_confidence",
        "opponent_breadth_confidence",
        "international_validation_confidence",
        "network_external_validation_confidence",
        "top3_domain_count", "win_domain_count",
        "effective_top3_domains", "effective_win_domains",
        "opponent_network_count", "effective_opponent_networks",
        "opponent_network_encounters",
        "foreign_top3_country_count", "foreign_win_country_count",
        "top3_domains", "win_domains",
    ]
    horse_cols = keys_horse + [
        "horse_venue_breadth_confidence",
        "horse_bridge_validation_confidence",
        "horse_international_validation_confidence",
        "horse_top3_domain_count", "horse_win_domain_count",
        "horse_effective_top3_domains", "horse_effective_win_domains",
        "horse_opponent_network_count", "horse_effective_opponent_networks",
        "horse_opponent_network_encounters",
        "horse_top3_domains", "horse_win_domains",
    ]

    out = dyn.merge(netc[net_cols], on=keys_net, how="left")
    out = out.merge(horsec[horse_cols], on=keys_horse, how="left")

    for col in [
        "internal_evidence_confidence",
        "network_external_validation_confidence",
        "horse_venue_breadth_confidence",
        "horse_bridge_validation_confidence",
        "horse_international_validation_confidence",
        "effective_top3_domains", "effective_win_domains",
        "effective_opponent_networks", "opponent_network_encounters",
        "horse_effective_top3_domains", "horse_effective_win_domains",
        "horse_effective_opponent_networks", "horse_opponent_network_encounters",
    ]:
        if col not in out:
            out[col] = 0.0
        out[col] = _num(out[col])

    out["horse_realized_top3_domain_share"] = _safe_ratio(
        out["horse_effective_top3_domains"], out["effective_top3_domains"], when_zero=0.0
    )
    out["horse_realized_win_domain_share"] = _safe_ratio(
        out["horse_effective_win_domains"], out["effective_win_domains"], when_zero=0.0
    )
    no_net_win = out["effective_win_domains"] <= 1e-12
    out.loc[no_net_win, "horse_realized_win_domain_share"] = out.loc[
        no_net_win, "horse_realized_top3_domain_share"
    ]
    out["horse_realized_venue_share"] = (
        0.65 * out["horse_realized_top3_domain_share"]
        + 0.35 * out["horse_realized_win_domain_share"]
    ).clip(0.0, 1.0)

    out["horse_realized_opponent_share"] = _safe_ratio(
        out["horse_effective_opponent_networks"],
        out["effective_opponent_networks"],
        when_zero=0.0,
    )
    out["horse_realized_opponent_encounter_share"] = _safe_ratio(
        out["horse_opponent_network_encounters"],
        out["opponent_network_encounters"],
        when_zero=0.0,
    )
    no_net_opp = out["effective_opponent_networks"] <= 1e-12
    out.loc[no_net_opp, "horse_realized_opponent_share"] = 0.0
    out.loc[no_net_opp, "horse_realized_opponent_encounter_share"] = 0.0
    out["horse_realized_opponent_validation"] = (
        0.70 * out["horse_realized_opponent_share"]
        + 0.30 * out["horse_realized_opponent_encounter_share"]
    ).clip(0.0, 1.0)

    out["horse_realized_network_use_confidence"] = (
        REALIZED_VENUE_WEIGHT * out["horse_realized_venue_share"]
        + REALIZED_OPPONENT_WEIGHT * out["horse_realized_opponent_validation"]
    ).clip(0.0, 1.0)

    out["network_inheritance_multiplier"] = (
        NETWORK_INHERITANCE_FLOOR
        + NETWORK_INHERITANCE_REALIZED_WEIGHT
        * out["horse_realized_network_use_confidence"]
    ).clip(0.0, 1.0)

    out["inherited_network_validation_confidence"] = (
        out["network_external_validation_confidence"]
        * out["network_inheritance_multiplier"]
    ).clip(0.0, 1.0)

    base_retention = (
        RETENTION_FLOOR
        + INTERNAL_WEIGHT * out["internal_evidence_confidence"]
        + INHERITED_NETWORK_WEIGHT * out["inherited_network_validation_confidence"]
        + HORSE_VENUE_WEIGHT * out["horse_venue_breadth_confidence"]
    )
    bridge_release = HORSE_BRIDGE_WEIGHT * out["horse_bridge_validation_confidence"]
    intl_release = HORSE_INTERNATIONAL_WEIGHT * out["horse_international_validation_confidence"]

    out["breadth_base_retention"] = np.clip(base_retention, 0.0, 1.0)
    out["horse_bridge_release"] = bridge_release
    out["horse_international_release"] = intl_release
    out["breadth_retention"] = np.clip(base_retention + bridge_release + intl_release, 0.0, 1.0)

    raw = pd.to_numeric(out["annual_surface_rating"], errors="coerce")
    out["breadth_calibrated_rating"] = 100.0 + out["breadth_retention"] * (raw - 100.0)
    out["breadth_ceiling_rating"] = np.where(
        raw > 100.0,
        100.0 + out["breadth_retention"] * (raw - 100.0),
        raw,
    )
    out["breadth_rating_change"] = out["breadth_ceiling_rating"] - raw

    out["breadth_racetype_surface_rank"] = (
        out.groupby(["year", "race_type", "surface_family"])["breadth_ceiling_rating"]
        .rank(method="min", ascending=False)
        .astype("Int64")
    )
    return out


def build_japan_ranking(ratings: pd.DataFrame) -> pd.DataFrame:
    j = ratings[
        pd.to_numeric(ratings["japan_top3_appearances"], errors="coerce").fillna(0) > 0
    ].copy()
    if j.empty:
        return j
    j["japan_breadth_rank"] = (
        j.groupby(["year", "race_type", "surface_family"])["breadth_ceiling_rating"]
        .rank(method="min", ascending=False)
        .astype(int)
    )
    return j.sort_values(
        ["year", "race_type", "surface_family", "japan_breadth_rank", "horse_pk"],
        kind="mergesort",
    ).reset_index(drop=True)


def atomic_csv(df: pd.DataFrame, path: Path):
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)


def write_report(
    path: Path,
    races_path: Path,
    results_dir: Path,
    mapped_races: int,
    netc: pd.DataFrame,
    ratings: pd.DataFrame,
    japan: pd.DataFrame,
    race_type_counts: dict,
):
    lines = [
        "Multi-Surface Flat/Jump Breadth Confidence Calibration",
        f"script: {SCRIPT_VERSION}",
        f"races: {races_path}",
        f"source results: {results_dir}",
        f"race-type rows: {race_type_counts}",
        f"mapped races with >=1 rated Top3 horse: {mapped_races:,}",
        f"networks scored: {len(netc):,}",
        f"horse-year-racetype-surface rows scored: {len(ratings):,}",
        "",
        "Hard identity key:",
        "  year x race_type x surface_family",
        "  FLAT and JUMP never share breadth/network calibration evidence.",
        "",
        "Grade-free rule:",
        "  race type uses race_name / comment / condition only.",
        "  grade / series_grade are NOT READ.",
        "",
        "Coefficients: unchanged from v1.1.1",
        f"  floor={RETENTION_FLOOR:.2f}",
        f"  + {INTERNAL_WEIGHT:.2f} * internal network evidence",
        f"  + {INHERITED_NETWORK_WEIGHT:.2f} * realized/inherited network validation",
        f"  + {HORSE_VENUE_WEIGHT:.2f} * horse venue breadth",
        f"  + up to {HORSE_BRIDGE_WEIGHT:.2f} horse cross-network bridge validation",
        f"  + up to {HORSE_INTERNATIONAL_WEIGHT:.2f} horse international validation",
        "",
        "Network inheritance:",
        "  broad network membership is not fully inherited automatically.",
        f"  inheritance multiplier = {NETWORK_INHERITANCE_FLOOR:.2f} + "
        f"{NETWORK_INHERITANCE_REALIZED_WEIGHT:.2f} * horse realized network use",
        "",
        "Selected Japan examples:",
    ]

    targets = {
        (1999, "MEISEI OPERA"),
        (1999, "SNOW ENDEAVOR"),
        (2017, "KITASAN BLACK"),
        (2017, "OJU CHOSAN"),
        (2023, "EQUINOX29"),
        (2023, "LEMON POP"),
        (2023, "MINIATURE17"),
        (2025, "OKEMARU"),
    }
    if not japan.empty:
        for r in japan.itertuples(index=False):
            if (int(r.year), str(r.horse_name).upper()) in targets:
                lines.append(
                    f"  {r.year} {r.race_type} {r.surface_family} {r.horse_name}: "
                    f"raw={r.annual_surface_rating:.3f} "
                    f"realized={r.horse_realized_network_use_confidence:.3f} "
                    f"retention={r.breadth_retention:.3f} "
                    f"calibrated={r.breadth_ceiling_rating:.3f} "
                    f"JapanRank={r.japan_breadth_rank}"
                )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(
        description="v1.2 Flat/Jump realized-breadth calibration"
    )
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--min-valid-year", type=int, default=1700)
    ap.add_argument("--max-valid-year", type=int, default=datetime.now().year)
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    races_path = split.core.discover_races(base_dir, args.races)
    results_dir = discover_results_dir(base_dir, args.results_dir)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else results_dir
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)

    print("=" * 78, flush=True)
    print(f"Flat/Jump Multi-Surface Breadth Confidence {SCRIPT_VERSION}", flush=True)
    print(f"races   : {races_path}", flush=True)
    print(f"results : {results_dir}", flush=True)
    print(f"output  : {output_dir}", flush=True)
    print("No BT/network refit: breadth post-processing only.", flush=True)
    print("v1.1.1 coefficients unchanged.", flush=True)
    print("=" * 78, flush=True)

    print("[1/6] reading v1.2 race-type dynamic ratings...", flush=True)
    dyn, netbase = read_required_results(results_dir)
    mapping = build_mapping(dyn)
    print(f"  rated horse-year-racetype-surface rows: {len(dyn):,}", flush=True)

    print("[2/6] reading races + classifying Flat/Jump grade-free...", flush=True)
    races = load_races(races_path, args.min_valid_year, args.max_valid_year)
    race_type_counts = races["race_type"].value_counts().to_dict()
    print(f"  race rows: {len(races):,}  types={race_type_counts}", flush=True)

    print("[3/6] measuring breadth inside race-type/surface cells...", flush=True)
    netbreadth, horsebreadth, mapped_races = build_breadth_tables(races, mapping)
    print(f"  mapped races: {mapped_races:,}", flush=True)

    print("[4/6] calculating v1.1.1 realized retention with race-type keys...", flush=True)
    net = netbase.merge(
        netbreadth,
        on=["year", "race_type", "surface_family", "dynamic_network_id"],
        how="left",
    )
    for col in netbreadth.columns:
        if col in {"year", "race_type", "surface_family", "dynamic_network_id"}:
            continue
        if col not in net:
            continue
        if pd.api.types.is_numeric_dtype(net[col]):
            net[col] = net[col].fillna(0)
        else:
            net[col] = net[col].fillna("")

    netc = base.add_network_confidence(net)
    horsec = base.add_horse_confidence(horsebreadth)
    ratings = calculate_ratings_realized(dyn, netc, horsec)
    japan = build_japan_ranking(ratings)

    out_network = output_dir / "grade_free_racetype_surface_network_breadth_confidence_v1_2_0.csv"
    out_rating = output_dir / "grade_free_racetype_surface_breadth_calibrated_ratings_v1_2_0.csv"
    out_japan = output_dir / "grade_free_japan_racetype_breadth_calibrated_ranking_v1_2_0.csv"
    out_horse = output_dir / "grade_free_racetype_horse_breadth_evidence_v1_2_0.csv"
    out_report = output_dir / "grade_free_racetype_breadth_calibration_report_v1_2_0.txt"

    print("[5/6] writing CSVs...", flush=True)
    atomic_csv(netc, out_network)
    atomic_csv(horsec, out_horse)
    atomic_csv(ratings, out_rating)
    atomic_csv(japan, out_japan)

    print("[6/6] writing report...", flush=True)
    write_report(
        out_report,
        races_path,
        results_dir,
        mapped_races,
        netc,
        ratings,
        japan,
        race_type_counts,
    )

    print("Done.", flush=True)
    for p in [out_network, out_horse, out_rating, out_japan, out_report]:
        print(f"  {p}", flush=True)


if __name__ == "__main__":
    main()
