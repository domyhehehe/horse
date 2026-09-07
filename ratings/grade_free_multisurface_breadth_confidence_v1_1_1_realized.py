#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_breadth_confidence_v1_1_1_realized.py
=============================================================

Refinement of v1.1.0 breadth-confidence calibration.

Main change
-----------
Do not let every horse fully inherit the breadth of its dynamic network.
Network breadth is discounted unless the horse itself actually used a meaningful
share of that network's venues / cross-network contacts.

The model still never adds rating points.  It only controls how much of a high
raw surface rating is retained instead of being shrunk toward 100.

Retention architecture
----------------------
  0.25 floor
+ 0.35 internal network evidence
+ 0.15 realized/inherited network external validation
+ 0.15 horse venue breadth
+ 0.20 horse cross-network bridge validation
+ 0.05 horse international validation

The network term is:

  inherited_network_validation
    = network_external_validation
      * (0.25 + 0.75 * horse_realized_network_use)

So a broad network is not automatically credited at 100% to every member.
A horse that personally spans the network's venues/opponents can inherit most of
that breadth; a horse that stays in a narrow slice inherits much less.

No JRA awards or external labels are used for fitting/tuning.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import grade_free_multisurface_breadth_confidence_v1_1_0 as base

SCRIPT_VERSION = "1.1.1-realized-breadth"
SCRIPT_DIR = Path(__file__).resolve().parent

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


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _safe_ratio(num: pd.Series, den: pd.Series, when_zero: float = 0.0) -> pd.Series:
    num = _num(num)
    den = _num(den)
    out = pd.Series(np.full(len(num), float(when_zero)), index=num.index, dtype=float)
    mask = den > 1e-12
    out.loc[mask] = num.loc[mask] / den.loc[mask]
    return out.clip(0.0, 1.0)


def calculate_ratings_realized(dyn: pd.DataFrame, netc: pd.DataFrame, horsec: pd.DataFrame) -> pd.DataFrame:
    keys_net = ["year", "surface_family", "dynamic_network_id"]
    keys_horse = ["year", "surface_family", "horse_pk"]

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

    # How much of the network's actual venue breadth did this horse personally use?
    out["horse_realized_top3_domain_share"] = _safe_ratio(
        out["horse_effective_top3_domains"], out["effective_top3_domains"], when_zero=0.0
    )
    out["horse_realized_win_domain_share"] = _safe_ratio(
        out["horse_effective_win_domains"], out["effective_win_domains"], when_zero=0.0
    )
    # If a network has no effective win-domain evidence, do not punish the horse for that absence.
    no_net_win = out["effective_win_domains"] <= 1e-12
    out.loc[no_net_win, "horse_realized_win_domain_share"] = out.loc[
        no_net_win, "horse_realized_top3_domain_share"
    ]
    out["horse_realized_venue_share"] = (
        0.65 * out["horse_realized_top3_domain_share"]
        + 0.35 * out["horse_realized_win_domain_share"]
    ).clip(0.0, 1.0)

    # Same idea for cross-network contacts.
    out["horse_realized_opponent_share"] = _safe_ratio(
        out["horse_effective_opponent_networks"], out["effective_opponent_networks"], when_zero=0.0
    )
    out["horse_realized_opponent_encounter_share"] = _safe_ratio(
        out["horse_opponent_network_encounters"], out["opponent_network_encounters"], when_zero=0.0
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
        + NETWORK_INHERITANCE_REALIZED_WEIGHT * out["horse_realized_network_use_confidence"]
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

    out["breadth_surface_rank"] = (
        out.groupby(["year", "surface_family"])["breadth_ceiling_rating"]
        .rank(method="min", ascending=False)
        .astype("Int64")
    )
    return out


def write_report(path: Path, races_path: Path, results_dir: Path,
                 mapped_races: int, netc: pd.DataFrame, ratings: pd.DataFrame,
                 japan: pd.DataFrame):
    lines = [
        "Multi-Surface Breadth Confidence Calibration",
        f"script: {SCRIPT_VERSION}",
        f"races: {races_path}",
        f"source results: {results_dir}",
        f"mapped races with >=1 rated Top3 horse: {mapped_races:,}",
        f"networks scored: {len(netc):,}",
        f"horse-year-surface rows scored: {len(ratings):,}",
        "",
        "Core rule:",
        "  No rating points are added.",
        "  High ratings are shrunk toward 100 when evidence is narrow.",
        "  Broad network membership is not fully inherited automatically.",
        "  Lack of international travel is not a direct penalty.",
        "",
        "Retention v1.1.1:",
        f"  floor={RETENTION_FLOOR:.2f}",
        f"  + {INTERNAL_WEIGHT:.2f} * internal network evidence",
        f"  + {INHERITED_NETWORK_WEIGHT:.2f} * realized/inherited network validation",
        f"  + {HORSE_VENUE_WEIGHT:.2f} * horse venue breadth",
        f"  + up to {HORSE_BRIDGE_WEIGHT:.2f} horse cross-network bridge validation",
        f"  + up to {HORSE_INTERNATIONAL_WEIGHT:.2f} horse international validation",
        "",
        "Network inheritance:",
        "  inherited_network_validation = network_external_validation",
        f"    * ({NETWORK_INHERITANCE_FLOOR:.2f} + {NETWORK_INHERITANCE_REALIZED_WEIGHT:.2f} * horse_realized_network_use)",
        f"  horse_realized_network_use = {REALIZED_VENUE_WEIGHT:.2f} venue-share + {REALIZED_OPPONENT_WEIGHT:.2f} opponent-share",
        "",
        "Track aliases applied:",
    ]
    for k, v in sorted(base.TRACK_ALIASES.items()):
        lines.append(f"  {k} -> {v}")

    if not japan.empty:
        lines += ["", "Selected Japan examples:"]
        targets = {
            (1999, "MEISEI OPERA"),
            (1999, "SNOW ENDEAVOR"),
            (2017, "KITASAN BLACK"),
            (2023, "EQUINOX29"),
            (2023, "LEMON POP"),
            (2023, "MINIATURE17"),
            (2025, "OKEMARU"),
        }
        for r in japan.itertuples(index=False):
            if (int(r.year), str(r.horse_name).upper()) in targets:
                realized = getattr(r, "horse_realized_network_use_confidence", np.nan)
                inherited = getattr(r, "inherited_network_validation_confidence", np.nan)
                lines.append(
                    f"  {r.year} {r.surface_family} {r.horse_name}: "
                    f"raw={r.annual_surface_rating:.3f} "
                    f"realized={realized:.3f} inheritedNet={inherited:.3f} "
                    f"retention={r.breadth_retention:.3f} "
                    f"calibrated={r.breadth_ceiling_rating:.3f} "
                    f"JapanRank={r.japan_breadth_rank}"
                )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Realized-breadth calibration for multi-surface ratings")
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--min-valid-year", type=int, default=1700)
    ap.add_argument("--max-valid-year", type=int, default=datetime.now().year)
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    races_path = base.discover_races(base_dir, args.races)
    results_dir = base.discover_results_dir(base_dir, args.results_dir)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else results_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)

    print("=" * 78, flush=True)
    print(f"Multi-Surface Breadth Confidence {SCRIPT_VERSION}", flush=True)
    print(f"races   : {races_path}", flush=True)
    print(f"results : {results_dir}", flush=True)
    print(f"output  : {output_dir}", flush=True)
    print("No BT/network refit: post-processing only.", flush=True)
    print("=" * 78, flush=True)

    print("[1/6] reading existing dynamic ratings...", flush=True)
    dyn, netbase = base.read_required_results(results_dir)
    mapping = base.build_mapping(dyn)
    print(f"  rated horse-year-surface rows: {len(dyn):,}", flush=True)

    print("[2/6] reading race rows and normalizing venue aliases...", flush=True)
    races = base.load_races(races_path, args.min_valid_year, args.max_valid_year)
    print(f"  race rows: {len(races):,}", flush=True)

    print("[3/6] measuring venue/opponent/international breadth...", flush=True)
    netbreadth, horsebreadth, mapped_races = base.build_breadth_tables(races, mapping)
    print(f"  mapped races: {mapped_races:,}", flush=True)

    print("[4/6] calculating realized breadth / retention...", flush=True)
    net = netbase.merge(
        netbreadth,
        on=["year", "surface_family", "dynamic_network_id"],
        how="left",
    )
    for col in netbreadth.columns:
        if col in {"year", "surface_family", "dynamic_network_id"} or col not in net:
            continue
        if pd.api.types.is_numeric_dtype(net[col]):
            net[col] = net[col].fillna(0)
        else:
            net[col] = net[col].fillna("")

    netc = base.add_network_confidence(net)
    horsec = base.add_horse_confidence(horsebreadth)
    ratings = calculate_ratings_realized(dyn, netc, horsec)
    japan = base.build_japan_ranking(ratings)

    out_network = output_dir / "grade_free_surface_network_breadth_confidence_v1_1_1.csv"
    out_rating = output_dir / "grade_free_surface_breadth_calibrated_ratings_v1_1_1.csv"
    out_japan = output_dir / "grade_free_japan_breadth_calibrated_ranking_v1_1_1.csv"
    out_horse = output_dir / "grade_free_horse_breadth_evidence_v1_1_1.csv"
    out_report = output_dir / "grade_free_breadth_calibration_report_v1_1_1.txt"

    print("[5/6] writing CSVs...", flush=True)
    base.atomic_csv(netc, out_network)
    base.atomic_csv(horsec, out_horse)
    base.atomic_csv(ratings, out_rating)
    base.atomic_csv(japan, out_japan)

    print("[6/6] writing report...", flush=True)
    write_report(out_report, races_path, results_dir, mapped_races, netc, ratings, japan)

    print("Done.", flush=True)
    for p in [out_network, out_horse, out_rating, out_japan, out_report]:
        print(f"  {p}", flush=True)


if __name__ == "__main__":
    main()
