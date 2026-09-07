#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_breadth_confidence_v1_1_0.py
====================================================

Post-process the v1.0.4/v1.0.5 multi-surface hard-split outputs with a
breadth/confidence shrinkage layer.

Goal
----
Keep the existing surface-specific rating model unchanged, but reduce
cross-network overstatement from small/narrow competition circuits.

This is NOT a bonus-point model.
A high raw surface rating is retained only to the extent that its network
has enough internal evidence and enough external/breadth validation.

Evidence dimensions
-------------------
1) Internal network evidence
   - horses
   - races
   - pairwise comparisons

2) Venue breadth
   - distinct/effective country x normalized-track domains with Top3 presence
   - distinct/effective domains with wins

3) Opponent-network breadth
   - how many other dynamic networks are encountered in Top3 races
   - effective opponent count and encounter count

4) International breadth
   - low-weight positive-only evidence
   - no foreign campaign is NOT a penalty

5) Horse-specific validation release
   - useful for horses such as Meisei Opera that personally bridge circuits
   - horse venue breadth + actual cross-network encounters
   - international evidence is again positive-only and low weight

The final retention is network-led.  Individual evidence can release some
shrinkage, but cannot create rating points.

Final high-side rating:
    100 + retention * (raw_rating - 100), if raw_rating > 100
    raw_rating,                              otherwise

A symmetric shrinkage value is also written for diagnostics.

No JRA awards or external labels are used to fit/tune the calculation.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

SCRIPT_VERSION = "1.1.0-breadth-confidence"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = "grade_free_multisurface_results_v1_0_4"

SURFACE_FAMILIES = ("TURF", "DIRT", "SYNTHETIC", "OTHER", "UNKNOWN")

# Saturation targets. These are evidence scales, not fitted performance labels.
INTERNAL_HORSE_TARGET = 150.0
INTERNAL_RACE_TARGET = 100.0
INTERNAL_COMPARISON_TARGET = 300.0

NETWORK_TOP3_DOMAIN_TARGET = 6.0
NETWORK_WIN_DOMAIN_TARGET = 5.0
NETWORK_OPPONENT_TARGET = 4.0
NETWORK_OPPONENT_ENCOUNTER_TARGET = 20.0

HORSE_TOP3_DOMAIN_TARGET = 4.0
HORSE_WIN_DOMAIN_TARGET = 3.0
HORSE_OPPONENT_TARGET = 3.0
HORSE_OPPONENT_ENCOUNTER_TARGET = 8.0

FOREIGN_TOP3_COUNTRY_TARGET = 3.0
FOREIGN_WIN_COUNTRY_TARGET = 2.0

# Retention architecture. Sum of base terms = 0.95 before optional releases.
RETENTION_FLOOR = 0.25
INTERNAL_WEIGHT = 0.40
NETWORK_VALIDATION_WEIGHT = 0.30
HORSE_VENUE_WEIGHT = 0.05
HORSE_BRIDGE_RELEASE_MAX = 0.10
HORSE_INTERNATIONAL_RELEASE_MAX = 0.05

# Network validation components.
NETWORK_VENUE_WEIGHT = 0.55
NETWORK_OPPONENT_WEIGHT = 0.30
NETWORK_INTERNATIONAL_WEIGHT = 0.15

# Venue components.
VENUE_TOP3_WEIGHT = 0.65
VENUE_WIN_WEIGHT = 0.35

# Opponent breadth components.
OPPONENT_EFFECTIVE_WEIGHT = 0.60
OPPONENT_ENCOUNTER_WEIGHT = 0.40

# Explicit aliases observed in the project. Keep this conservative/editable.
TRACK_ALIASES = {
    "TOKYO, FUCHU": "TOKYO",
    "TOKYO FUCHU": "TOKYO",
    "FUCHU": "TOKYO",
    "MIZASAWA": "MIZUSAWA",
    "SHA TIN, HK": "SHA TIN",
    "SHA TIN HK": "SHA TIN",
    "MEYDAN, DUBAI": "MEYDAN",
}

RACE_USECOLS = [
    "race_id", "year", "country", "track", "surface",
    "winner_pk", "second_pk", "third_pk",
]


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return str(v).strip()


def _norm(v) -> str:
    return " ".join(unicodedata.normalize("NFKC", _txt(v)).upper().split())


def normalize_pk(v) -> str:
    return _txt(v).lower().strip()


def normalize_country(v) -> str:
    x = _norm(v)
    if x in {"JPN", "JAPAN", "日本", "NIPPON"}:
        return "JAPAN"
    if x in {"USA", "US", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "UNITED STATES"
    if x in {"GB", "UK", "GREAT BRITAIN", "UNITED KINGDOM", "ENGLAND"}:
        return "GREAT BRITAIN"
    if x in {"IRE", "IRELAND"}:
        return "IRELAND"
    if x in {"FR", "FRANCE"}:
        return "FRANCE"
    return x or "UNKNOWN"


def surface_family(v) -> str:
    raw = _norm(v)
    compact = re.sub(r"[^A-Z0-9一-龠ぁ-んァ-ヶ]+", " ", raw).strip()
    compact_no_space = compact.replace(" ", "")
    if not compact or compact in {"UNKNOWN", "UNK", "N A", "NA", "?", "NONE"}:
        return "UNKNOWN"
    synth = (
        "SYNTHETIC", "SYNTH", "ALL WEATHER", "ALLWEATHER", "POLYTRACK",
        "TAPETA", "CUSHION TRACK", "CUSHION", "FIBRESAND", "FIBERSAND",
        "PRO RIDE", "PRORIDE", "VISCO RIDE", "ARTIFICIAL",
    )
    if any(t in compact for t in synth) or compact_no_space in {"AW", "AWT"}:
        return "SYNTHETIC"
    if "TURF" in compact or "GRASS" in compact or "芝" in raw:
        return "TURF"
    if "DIRT" in compact or "SAND" in compact or "ダート" in raw:
        return "DIRT"
    return "OTHER"


def normalize_track(v) -> str:
    x = _norm(v)
    if not x:
        return "UNKNOWN"
    x = re.sub(r"\s*,\s*", ", ", x)
    x = re.sub(r"\s+", " ", x).strip()
    if x in TRACK_ALIASES:
        return TRACK_ALIASES[x]
    x2 = x.replace(",", "")
    x2 = re.sub(r"\s+", " ", x2).strip()
    if x2 in TRACK_ALIASES:
        return TRACK_ALIASES[x2]
    return x


def domain_name(country: str, track: str) -> str:
    return f"{country}::{track}"


def effective_count(counter: Counter) -> float:
    vals = np.asarray([float(v) for v in counter.values() if v > 0], dtype=float)
    if vals.size == 0:
        return 0.0
    p = vals / vals.sum()
    return float(math.exp(-np.sum(p * np.log(p))))


def linear_scale_effective(value: float, target: float) -> float:
    """1 domain => 0 external breadth; target domains => 1."""
    if value <= 1.0:
        return 0.0
    return float(np.clip((value - 1.0) / max(1e-9, target - 1.0), 0.0, 1.0))


def log_saturation(value: float, target: float) -> float:
    if value <= 0:
        return 0.0
    return float(np.clip(math.log1p(value) / math.log1p(target), 0.0, 1.0))


def discover_races(base_dir: Path, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    exact = base_dir / "stakes_races_one_row.csv"
    if exact.exists():
        return exact
    candidates = sorted(
        base_dir.glob("stakes_races_one_row*.csv"),
        key=lambda p: (p.stat().st_mtime, p.stat().st_size),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("stakes_races_one_row*.csv not found")
    return candidates[0]


def discover_results_dir(base_dir: Path, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    preferred = base_dir / DEFAULT_RESULTS_DIR
    if (preferred / "grade_free_surface_dynamic_network_ratings.csv").exists():
        return preferred
    if (base_dir / "grade_free_surface_dynamic_network_ratings.csv").exists():
        return base_dir
    candidates = []
    for p in base_dir.glob("grade_free_multisurface_results*"):
        if p.is_dir() and (p / "grade_free_surface_dynamic_network_ratings.csv").exists():
            candidates.append(p)
    if not candidates:
        raise FileNotFoundError("Could not find multi-surface result directory")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def read_required_results(results_dir: Path):
    dyn_path = results_dir / "grade_free_surface_dynamic_network_ratings.csv"
    sum_path = results_dir / "grade_free_surface_dynamic_network_summary.csv"
    if not dyn_path.exists() or not sum_path.exists():
        raise FileNotFoundError("dynamic rating/summary CSV missing from results dir")

    dyn_cols = [
        "year", "surface_family", "horse_pk", "horse_name",
        "annual_surface_rating", "dynamic_network_id",
        "dynamic_network_horses", "dynamic_network_races",
        "japan_top3_appearances",
    ]
    sum_cols = [
        "year", "surface_family", "dynamic_network_id",
        "dynamic_network_horses", "dynamic_network_races",
        "internal_comparisons", "external_comparisons",
        "bridge_horses", "conductance", "dominant_country",
    ]
    dyn = pd.read_csv(dyn_path, usecols=dyn_cols, low_memory=False)
    net = pd.read_csv(sum_path, usecols=sum_cols, low_memory=False)
    dyn["horse_pk"] = dyn["horse_pk"].map(normalize_pk)
    dyn["year"] = pd.to_numeric(dyn["year"], errors="coerce").astype("Int64")
    net["year"] = pd.to_numeric(net["year"], errors="coerce").astype("Int64")
    dyn = dyn[dyn.year.notna()].copy()
    net = net[net.year.notna()].copy()
    dyn["year"] = dyn["year"].astype(int)
    net["year"] = net["year"].astype(int)
    dyn["dynamic_network_id"] = pd.to_numeric(dyn["dynamic_network_id"], errors="coerce").astype("Int64")
    net["dynamic_network_id"] = pd.to_numeric(net["dynamic_network_id"], errors="coerce").astype("Int64")
    return dyn, net


def build_mapping(dyn: pd.DataFrame) -> Dict[Tuple[int, str, str], int]:
    out = {}
    for r in dyn[["year", "surface_family", "horse_pk", "dynamic_network_id"]].itertuples(index=False):
        if pd.isna(r.dynamic_network_id):
            continue
        out[(int(r.year), str(r.surface_family), normalize_pk(r.horse_pk))] = int(r.dynamic_network_id)
    return out


def load_races(races_path: Path, min_year: int, max_year: int) -> pd.DataFrame:
    df = pd.read_csv(races_path, usecols=RACE_USECOLS, dtype=str, low_memory=False)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df[df.year.notna()].copy()
    df["year"] = df["year"].astype(int)
    df = df[(df.year >= min_year) & (df.year <= max_year)].copy()
    df["surface_family_norm"] = df["surface"].map(surface_family)
    df["country_norm"] = df["country"].map(normalize_country)
    df["track_norm"] = df["track"].map(normalize_track)
    df["domain_norm"] = [
        domain_name(c, t) for c, t in zip(df["country_norm"].astype(str), df["track_norm"].astype(str))
    ]
    return df


def build_breadth_tables(races: pd.DataFrame, mapping: Dict[Tuple[int, str, str], int]):
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
        fam = str(row.surface_family_norm)
        country = str(row.country_norm)
        domain = str(row.domain_norm)

        pks = [
            normalize_pk(row.winner_pk),
            normalize_pk(row.second_pk),
            normalize_pk(row.third_pk),
        ]
        entries = []
        for placing, pk in enumerate(pks, start=1):
            if not pk:
                continue
            nid = mapping.get((year, fam, pk))
            if nid is None:
                continue
            entries.append((placing, pk, nid))
        if not entries:
            continue
        mapped_races += 1

        networks = sorted({nid for _, _, nid in entries})
        for nid in networks:
            nk = (year, fam, nid)
            net_top3_domain[nk][domain] += 1
            net_top3_country[nk][country] += 1

        winner_entries = [e for e in entries if e[0] == 1]
        if winner_entries:
            _placing, winner_pk, winner_nid = winner_entries[0]
            nk = (year, fam, winner_nid)
            net_win_domain[nk][domain] += 1
            net_win_country[nk][country] += 1

        if len(networks) >= 2:
            for a in networks:
                ak = (year, fam, a)
                for b in networks:
                    if b != a:
                        net_opponents[ak][b] += 1

        for placing, pk, nid in entries:
            hk = (year, fam, pk)
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
        year, fam, nid = nk
        td = net_top3_domain[nk]
        wd = net_win_domain[nk]
        tc = net_top3_country[nk]
        wc = net_win_country[nk]
        op = net_opponents[nk]
        network_rows.append({
            "year": year,
            "surface_family": fam,
            "dynamic_network_id": nid,
            "top3_domain_count": len(td),
            "win_domain_count": len(wd),
            "effective_top3_domains": effective_count(td),
            "effective_win_domains": effective_count(wd),
            "top3_country_count": len(tc),
            "win_country_count": len(wc),
            "effective_top3_countries": effective_count(tc),
            "effective_win_countries": effective_count(wc),
            "opponent_network_count": len(op),
            "effective_opponent_networks": effective_count(op),
            "opponent_network_encounters": int(sum(op.values())),
            "top3_domains": " | ".join(f"{k}:{v}" for k, v in td.most_common(12)),
            "win_domains": " | ".join(f"{k}:{v}" for k, v in wd.most_common(12)),
        })

    horse_keys = set(horse_top3_domain) | set(horse_win_domain) | set(horse_opponents)
    horse_rows = []
    for hk in sorted(horse_keys):
        year, fam, pk = hk
        td = horse_top3_domain[hk]
        wd = horse_win_domain[hk]
        tc = horse_top3_country[hk]
        wc = horse_win_country[hk]
        op = horse_opponents[hk]
        horse_rows.append({
            "year": year,
            "surface_family": fam,
            "horse_pk": pk,
            "horse_top3_domain_count": len(td),
            "horse_win_domain_count": len(wd),
            "horse_effective_top3_domains": effective_count(td),
            "horse_effective_win_domains": effective_count(wd),
            "horse_top3_country_count": len(tc),
            "horse_win_country_count": len(wc),
            "horse_effective_top3_countries": effective_count(tc),
            "horse_effective_win_countries": effective_count(wc),
            "horse_opponent_network_count": len(op),
            "horse_effective_opponent_networks": effective_count(op),
            "horse_opponent_network_encounters": int(sum(op.values())),
            "horse_top3_domains": " | ".join(f"{k}:{v}" for k, v in td.most_common(10)),
            "horse_win_domains": " | ".join(f"{k}:{v}" for k, v in wd.most_common(10)),
        })

    return pd.DataFrame(network_rows), pd.DataFrame(horse_rows), mapped_races


def add_network_confidence(net: pd.DataFrame) -> pd.DataFrame:
    x = net.copy()
    for col in [
        "dynamic_network_horses", "dynamic_network_races",
        "internal_comparisons", "external_comparisons",
        "top3_domain_count", "win_domain_count",
        "effective_top3_domains", "effective_win_domains",
        "top3_country_count", "win_country_count",
        "effective_top3_countries", "effective_win_countries",
        "opponent_network_count", "effective_opponent_networks",
        "opponent_network_encounters",
    ]:
        if col not in x:
            x[col] = 0
        x[col] = pd.to_numeric(x[col], errors="coerce").fillna(0)

    x["internal_horse_confidence"] = x["dynamic_network_horses"].map(
        lambda v: log_saturation(v, INTERNAL_HORSE_TARGET)
    )
    x["internal_race_confidence"] = x["dynamic_network_races"].map(
        lambda v: log_saturation(v, INTERNAL_RACE_TARGET)
    )
    total_comps = x["internal_comparisons"] + x["external_comparisons"]
    x["internal_comparison_confidence"] = total_comps.map(
        lambda v: log_saturation(v, INTERNAL_COMPARISON_TARGET)
    )
    x["internal_evidence_confidence"] = (
        0.45 * x["internal_horse_confidence"]
        + 0.30 * x["internal_race_confidence"]
        + 0.25 * x["internal_comparison_confidence"]
    )

    x["venue_top3_confidence"] = x["effective_top3_domains"].map(
        lambda v: linear_scale_effective(v, NETWORK_TOP3_DOMAIN_TARGET)
    )
    x["venue_win_confidence"] = x["effective_win_domains"].map(
        lambda v: linear_scale_effective(v, NETWORK_WIN_DOMAIN_TARGET)
    )
    x["venue_breadth_confidence"] = (
        VENUE_TOP3_WEIGHT * x["venue_top3_confidence"]
        + VENUE_WIN_WEIGHT * x["venue_win_confidence"]
    )

    x["opponent_effective_confidence"] = x["effective_opponent_networks"].map(
        lambda v: linear_scale_effective(v, NETWORK_OPPONENT_TARGET)
    )
    x["opponent_encounter_confidence"] = x["opponent_network_encounters"].map(
        lambda v: log_saturation(v, NETWORK_OPPONENT_ENCOUNTER_TARGET)
    )
    x["opponent_breadth_confidence"] = (
        OPPONENT_EFFECTIVE_WEIGHT * x["opponent_effective_confidence"]
        + OPPONENT_ENCOUNTER_WEIGHT * x["opponent_encounter_confidence"]
    )

    # Positive-only. The dominant market itself occupies one country slot.
    x["foreign_top3_country_count"] = np.maximum(0, x["top3_country_count"] - 1)
    x["foreign_win_country_count"] = np.maximum(0, x["win_country_count"] - 1)
    x["international_top3_confidence"] = x["foreign_top3_country_count"].map(
        lambda v: float(np.clip(v / FOREIGN_TOP3_COUNTRY_TARGET, 0.0, 1.0))
    )
    x["international_win_confidence"] = x["foreign_win_country_count"].map(
        lambda v: float(np.clip(v / FOREIGN_WIN_COUNTRY_TARGET, 0.0, 1.0))
    )
    x["international_validation_confidence"] = (
        0.60 * x["international_top3_confidence"]
        + 0.40 * x["international_win_confidence"]
    )

    x["network_external_validation_confidence"] = (
        NETWORK_VENUE_WEIGHT * x["venue_breadth_confidence"]
        + NETWORK_OPPONENT_WEIGHT * x["opponent_breadth_confidence"]
        + NETWORK_INTERNATIONAL_WEIGHT * x["international_validation_confidence"]
    )
    return x


def add_horse_confidence(h: pd.DataFrame) -> pd.DataFrame:
    x = h.copy()
    for col in [
        "horse_effective_top3_domains", "horse_effective_win_domains",
        "horse_top3_country_count", "horse_win_country_count",
        "horse_effective_opponent_networks", "horse_opponent_network_encounters",
    ]:
        if col not in x:
            x[col] = 0
        x[col] = pd.to_numeric(x[col], errors="coerce").fillna(0)

    x["horse_venue_top3_confidence"] = x["horse_effective_top3_domains"].map(
        lambda v: linear_scale_effective(v, HORSE_TOP3_DOMAIN_TARGET)
    )
    x["horse_venue_win_confidence"] = x["horse_effective_win_domains"].map(
        lambda v: linear_scale_effective(v, HORSE_WIN_DOMAIN_TARGET)
    )
    x["horse_venue_breadth_confidence"] = (
        VENUE_TOP3_WEIGHT * x["horse_venue_top3_confidence"]
        + VENUE_WIN_WEIGHT * x["horse_venue_win_confidence"]
    )

    x["horse_opponent_effective_confidence"] = x["horse_effective_opponent_networks"].map(
        lambda v: linear_scale_effective(v, HORSE_OPPONENT_TARGET)
    )
    x["horse_opponent_encounter_confidence"] = x["horse_opponent_network_encounters"].map(
        lambda v: log_saturation(v, HORSE_OPPONENT_ENCOUNTER_TARGET)
    )
    x["horse_bridge_validation_confidence"] = (
        OPPONENT_EFFECTIVE_WEIGHT * x["horse_opponent_effective_confidence"]
        + OPPONENT_ENCOUNTER_WEIGHT * x["horse_opponent_encounter_confidence"]
    )

    x["horse_foreign_top3_country_count"] = np.maximum(0, x["horse_top3_country_count"] - 1)
    x["horse_foreign_win_country_count"] = np.maximum(0, x["horse_win_country_count"] - 1)
    x["horse_international_validation_confidence"] = (
        0.60 * np.clip(x["horse_foreign_top3_country_count"] / FOREIGN_TOP3_COUNTRY_TARGET, 0, 1)
        + 0.40 * np.clip(x["horse_foreign_win_country_count"] / FOREIGN_WIN_COUNTRY_TARGET, 0, 1)
    )
    return x


def calculate_ratings(dyn: pd.DataFrame, netc: pd.DataFrame, horsec: pd.DataFrame) -> pd.DataFrame:
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

    fill_conf = [
        "internal_evidence_confidence", "network_external_validation_confidence",
        "horse_venue_breadth_confidence", "horse_bridge_validation_confidence",
        "horse_international_validation_confidence",
    ]
    for col in fill_conf:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    base = (
        RETENTION_FLOOR
        + INTERNAL_WEIGHT * out["internal_evidence_confidence"]
        + NETWORK_VALIDATION_WEIGHT * out["network_external_validation_confidence"]
        + HORSE_VENUE_WEIGHT * out["horse_venue_breadth_confidence"]
    )
    bridge_release = HORSE_BRIDGE_RELEASE_MAX * out["horse_bridge_validation_confidence"]
    intl_release = HORSE_INTERNATIONAL_RELEASE_MAX * out["horse_international_validation_confidence"]

    out["breadth_base_retention"] = np.clip(base, 0.0, 1.0)
    out["horse_bridge_release"] = bridge_release
    out["horse_international_release"] = intl_release
    out["breadth_retention"] = np.clip(base + bridge_release + intl_release, 0.0, 1.0)

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


def build_japan_ranking(ratings: pd.DataFrame) -> pd.DataFrame:
    j = ratings[pd.to_numeric(ratings["japan_top3_appearances"], errors="coerce").fillna(0) > 0].copy()
    if j.empty:
        return j
    j["japan_breadth_rank"] = (
        j.groupby(["year", "surface_family"])["breadth_ceiling_rating"]
        .rank(method="min", ascending=False)
        .astype(int)
    )
    return j.sort_values(
        ["year", "surface_family", "japan_breadth_rank", "horse_pk"],
        kind="mergesort",
    ).reset_index(drop=True)


def atomic_csv(df: pd.DataFrame, path: Path):
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)


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
        "  Lack of international travel is not a direct penalty.",
        "",
        "Retention:",
        f"  floor={RETENTION_FLOOR:.2f}",
        f"  + {INTERNAL_WEIGHT:.2f} * internal evidence",
        f"  + {NETWORK_VALIDATION_WEIGHT:.2f} * network external validation",
        f"  + {HORSE_VENUE_WEIGHT:.2f} * horse venue breadth",
        f"  + up to {HORSE_BRIDGE_RELEASE_MAX:.2f} horse cross-network release",
        f"  + up to {HORSE_INTERNATIONAL_RELEASE_MAX:.2f} horse international release",
        "",
        "Network external validation:",
        f"  venue {NETWORK_VENUE_WEIGHT:.2f}",
        f"  opponent-network breadth {NETWORK_OPPONENT_WEIGHT:.2f}",
        f"  international breadth {NETWORK_INTERNATIONAL_WEIGHT:.2f}",
        "",
        "Track aliases applied:",
    ]
    for k, v in sorted(TRACK_ALIASES.items()):
        lines.append(f"  {k} -> {v}")

    if not japan.empty:
        lines += ["", "Selected Japan examples:"]
        targets = {
            (1999, "MEISEI OPERA"),
            (2017, "KITASAN BLACK"),
            (2023, "EQUINOX29"),
            (2023, "LEMON POP"),
            (2023, "MINIATURE17"),
            (2025, "OKEMARU"),
        }
        for r in japan.itertuples(index=False):
            if (int(r.year), str(r.horse_name).upper()) in targets:
                lines.append(
                    f"  {r.year} {r.surface_family} {r.horse_name}: "
                    f"raw={r.annual_surface_rating:.3f} "
                    f"retention={r.breadth_retention:.3f} "
                    f"calibrated={r.breadth_ceiling_rating:.3f} "
                    f"JapanRank={r.japan_breadth_rank}"
                )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Breadth-confidence calibration for multi-surface ratings")
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR))
    ap.add_argument("--races", default=None)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--min-valid-year", type=int, default=1700)
    ap.add_argument("--max-valid-year", type=int, default=datetime.now().year)
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    races_path = discover_races(base_dir, args.races)
    results_dir = discover_results_dir(base_dir, args.results_dir)
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
    dyn, netbase = read_required_results(results_dir)
    mapping = build_mapping(dyn)
    print(f"  rated horse-year-surface rows: {len(dyn):,}", flush=True)

    print("[2/6] reading race rows and normalizing venue aliases...", flush=True)
    races = load_races(races_path, args.min_valid_year, args.max_valid_year)
    print(f"  race rows: {len(races):,}", flush=True)

    print("[3/6] measuring venue/opponent/international breadth...", flush=True)
    netbreadth, horsebreadth, mapped_races = build_breadth_tables(races, mapping)
    print(f"  mapped races: {mapped_races:,}", flush=True)

    print("[4/6] calculating confidence/retention...", flush=True)
    net = netbase.merge(
        netbreadth,
        on=["year", "surface_family", "dynamic_network_id"],
        how="left",
    )
    for col in netbreadth.columns:
        if col in {"year", "surface_family", "dynamic_network_id"}:
            continue
        if col not in net:
            continue
        if pd.api.types.is_numeric_dtype(net[col]):
            net[col] = net[col].fillna(0)
        else:
            net[col] = net[col].fillna("")
    netc = add_network_confidence(net)
    horsec = add_horse_confidence(horsebreadth)
    ratings = calculate_ratings(dyn, netc, horsec)
    japan = build_japan_ranking(ratings)

    out_network = output_dir / "grade_free_surface_network_breadth_confidence.csv"
    out_rating = output_dir / "grade_free_surface_breadth_calibrated_ratings.csv"
    out_japan = output_dir / "grade_free_japan_breadth_calibrated_ranking.csv"
    out_horse = output_dir / "grade_free_horse_breadth_evidence.csv"
    out_report = output_dir / "grade_free_breadth_calibration_report.txt"

    print("[5/6] writing CSVs...", flush=True)
    atomic_csv(netc, out_network)
    atomic_csv(horsec, out_horse)
    atomic_csv(ratings, out_rating)
    atomic_csv(japan, out_japan)

    print("[6/6] writing report...", flush=True)
    write_report(out_report, races_path, results_dir, mapped_races, netc, ratings, japan)

    print("Done.", flush=True)
    for p in [out_network, out_horse, out_rating, out_japan, out_report]:
        print(f"  {p}", flush=True)


if __name__ == "__main__":
    main()
