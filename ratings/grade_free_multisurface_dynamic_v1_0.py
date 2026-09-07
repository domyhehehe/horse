#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_dynamic_v1_0.py
======================================

Experimental grade-free horse rating with a HARD surface-family split.

Purpose
-------
The existing grade-free annual rating can connect Turf, Dirt and synthetic races
through horses that run on more than one surface.  This experiment deliberately
cuts that path BEFORE fitting the annual horse model:

    TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN

For each calendar year and surface family independently it:
  1. builds Top2/Top3 ordered comparisons;
  2. fits an L2 Bradley-Terry model;
  3. performs 5-fold race-level cross-fitting;
  4. builds opponent-only context for each horse;
  5. applies the same asymmetric context idea used in the current grade-free
     horse model (positive gap: 45% x 2nd/3rd share, cap +24;
     negative gap: 10% x win share, floor -8);
  6. shrinks by comparison/context evidence toward 100;
  7. recenters the FINAL surface-specific horse ratings to mean 100 inside each
     year x surface family;
  8. finds dynamic competition networks ONLY inside that surface family;
  9. writes multi-surface horse summaries without averaging the surface ratings.

Important interpretation
------------------------
* Surface ratings are standardized separately.  A TURF 123 and DIRT 123 are
  both strong relative positions inside their own annual surface populations;
  this script does NOT claim that the two numbers are an absolute cross-surface
  physical scale.
* Primary Strength is therefore a descriptive maximum of the reliable
  surface-relative ratings, NOT a final Turf-vs-Dirt absolute comparison.
* Multi-Surface Floor is the minimum of the reliable surface-relative ratings
  when a horse has >=2 sufficiently evidenced surfaces.  It is intended to
  identify horses such as Kurofune / Agnes Digital that remain strong even on
  their weaker surface, not to penalize one-surface specialists.
* No grade / series_grade field is read or used anywhere.
* Country, track and raw surface labels are used only AFTER fitting/partitioning
  for explanatory breadth summaries and Japan-host ranking views.

Synthetic / All-Weather handling
--------------------------------
Synthetic is NOT silently merged into Dirt.  Common labels such as AW,
All-Weather, Polytrack, Tapeta, Cushion Track, Fibresand/Fibersand and Pro-Ride
are classified as SYNTHETIC.  Rare non-empty surfaces become OTHER.  Empty or
unknown labels become UNKNOWN.

A cross-surface overlap table is also written.  It uses horses that have
reliable ratings on two surfaces in the same year to show empirical overlap,
correlation and rating gaps.  This is a diagnostic for later deciding whether a
particular synthetic population behaves more like Turf or Dirt; it is NOT used
inside the rating fit.

Inputs
------
Expected beside this script unless --races is supplied:
    stakes_races_one_row.csv
(or a filename beginning stakes_races_one_row and ending .csv)

Outputs (fixed default folder)
------------------------------
    grade_free_multisurface_results_v1_0/
      grade_free_surface_horse_ratings.csv
      grade_free_surface_dynamic_network_ratings.csv
      grade_free_surface_dynamic_network_summary.csv
      grade_free_horse_multisurface_summary.csv
      grade_free_surface_overlap_matrix.csv
      grade_free_japan_surface_ranking.csv
      grade_free_multisurface_report.txt

Dependencies
------------
Python 3.10+, numpy, pandas, scipy.
Designed for Pyto / desktop Python.  No networkx dependency.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from scipy.optimize import minimize
except Exception as exc:  # pragma: no cover
    raise SystemExit("scipy is required for the Bradley-Terry fit: %s" % exc)


SCRIPT_VERSION = "1.0.0-multisurface-hard-split"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = "grade_free_multisurface_results_v1_0"

# Same broad horse-rating behavior as the current opponent-only model.
CROSSFIT_FOLDS = 5
BT_L2 = 1.0
POSITIVE_CONTEXT_RATE = 0.45
NEGATIVE_CONTEXT_RATE = 0.10
POSITIVE_CONTEXT_CAP = 24.0
NEGATIVE_CONTEXT_FLOOR = -8.0
PRIOR_EVIDENCE = 4.0
PLACE_ADJUSTMENT = {1: 3.0, 2: 0.0, 3: -3.0}

# Dynamic-network rules.
DYNAMIC_MIN_PARENT_HORSES = 30
DYNAMIC_RESOLUTION = 1.0
DYNAMIC_MAX_ACCEPTED_CONDUCTANCE = 0.20
DYNAMIC_NODE_SWEEPS = 25
DYNAMIC_MERGE_ROUNDS = 30

# Multi-surface summary: this only decides whether a surface is trusted enough
# to enter Primary Strength / Multi-Surface Floor.  It does not change ratings.
RELIABLE_SURFACE_COMPARISON_EVIDENCE = 4
RELIABLE_SURFACE_TOP3_APPEARANCES = 2

DUMMY_PK_TOKENS = {
    "", "unknown", "xx", "x", "na", "n/a", "none", "null", "?",
    "need winners name", "need winner name", "winner unknown",
}

RACE_USECOLS = [
    "race_id", "year", "race_name", "country", "track", "surface",
    "winner_name", "winner_pk", "second_name", "second_pk",
    "third_name", "third_pk",
]

SURFACE_FAMILIES = ("TURF", "DIRT", "SYNTHETIC", "OTHER", "UNKNOWN")


# ---------------------------------------------------------------------------
# Basic normalization
# ---------------------------------------------------------------------------

def _txt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _norm(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", _txt(value)).upper().split())


def normalize_pk(value) -> str:
    return _txt(value).strip().lower()


def valid_pk(value) -> bool:
    pk = normalize_pk(value)
    if pk in DUMMY_PK_TOKENS:
        return False
    compact = re.sub(r"[\s_+\-]+", " ", pk).strip()
    if compact in DUMMY_PK_TOKENS:
        return False
    if compact.startswith("need ") and "name" in compact:
        return False
    return True


def normalize_country(value) -> str:
    x = _norm(value)
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


def surface_family(value) -> str:
    """Conservative hard classification.  Unknown never becomes Dirt/Turf."""
    raw = _norm(value)
    compact = re.sub(r"[^A-Z0-9一-龠ぁ-んァ-ヶ]+", " ", raw).strip()
    if not compact or compact in {"UNKNOWN", "UNK", "N A", "NA", "?", "NONE"}:
        return "UNKNOWN"

    # Synthetic first so e.g. 'synthetic dirt' does not fall into DIRT.
    synth_tokens = (
        "SYNTHETIC", "ALL WEATHER", "ALLWEATHER", "A W", "POLYTRACK",
        "TAPETA", "CUSHION TRACK", "CUSHION", "FIBRESAND", "FIBERSAND",
        "PRO RIDE", "PRORIDE", "VISCO RIDE", "ARTIFICIAL",
    )
    if any(tok in compact for tok in synth_tokens):
        return "SYNTHETIC"

    if "TURF" in compact or "GRASS" in compact or "芝" in raw:
        return "TURF"
    if "DIRT" in compact or "ダート" in raw:
        return "DIRT"

    return "OTHER"


def stable_fold(race_id: str, folds: int = CROSSFIT_FOLDS) -> int:
    h = hashlib.blake2b(_txt(race_id).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % folds


def discover_races(base_dir: Path, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    exact = base_dir / "stakes_races_one_row.csv"
    if exact.exists():
        return exact
    candidates = sorted(base_dir.glob("stakes_races_one_row*.csv"), key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
    if not candidates:
        raise FileNotFoundError("stakes_races_one_row*.csv was not found beside the script")
    return candidates[0]


def percentile_from_rank(rank: int, n: int) -> float:
    if n <= 1:
        return 100.0
    return 100.0 * (n - rank) / (n - 1)


def weighted_mean(items: Sequence[Tuple[float, float]]) -> Optional[float]:
    if not items:
        return None
    sw = sum(w for _, w in items if w > 0)
    if sw <= 0:
        return None
    return sum(v * w for v, w in items if w > 0) / sw


def standardize_dict(values: Dict[str, float], mean_target=100.0, sd_target=10.0) -> Dict[str, float]:
    clean = [float(v) for v in values.values() if v is not None and math.isfinite(float(v))]
    if not clean:
        return {}
    mu = float(np.mean(clean))
    sd = float(np.std(clean, ddof=0))
    if sd <= 1e-12:
        return {k: mean_target for k in values}
    return {k: mean_target + sd_target * ((float(v) - mu) / sd) for k, v in values.items()}


# ---------------------------------------------------------------------------
# Bradley-Terry
# ---------------------------------------------------------------------------

def fit_bt(comparisons: Sequence[Tuple[str, str]], l2: float = BT_L2) -> Tuple[Dict[str, float], Dict[str, int], bool, str]:
    horses = sorted({p for pair in comparisons for p in pair})
    if len(horses) < 2 or not comparisons:
        return {}, {}, False, "insufficient comparisons"

    idx = {pk: i for i, pk in enumerate(horses)}
    wi = np.fromiter((idx[a] for a, _ in comparisons), dtype=np.int64)
    li = np.fromiter((idx[b] for _, b in comparisons), dtype=np.int64)
    n = len(horses)

    evidence = np.zeros(n, dtype=np.int64)
    np.add.at(evidence, wi, 1)
    np.add.at(evidence, li, 1)

    def fg(x):
        d = x[wi] - x[li]
        loss = float(np.logaddexp(0.0, -d).sum() + 0.5 * l2 * np.dot(x, x))
        # q = sigmoid(-d), derivative: winner -= q, loser += q
        q = np.empty_like(d)
        pos = d >= 0
        q[pos] = np.exp(-d[pos]) / (1.0 + np.exp(-d[pos]))
        q[~pos] = 1.0 / (1.0 + np.exp(d[~pos]))
        g = l2 * x.copy()
        np.add.at(g, wi, -q)
        np.add.at(g, li, q)
        return loss, g

    x0 = np.zeros(n, dtype=float)
    result = minimize(lambda x: fg(x), x0, method="L-BFGS-B", jac=True, options={"maxiter": 500, "ftol": 1e-11})
    x = np.asarray(result.x, dtype=float)
    x -= float(np.mean(x))
    scores = {pk: float(x[idx[pk]]) for pk in horses}
    ev = {pk: int(evidence[idx[pk]]) for pk in horses}
    return scores, ev, bool(result.success), str(result.message)


def bt_to_rating(scores: Dict[str, float], evidence: Dict[str, int]) -> Dict[str, float]:
    eligible = {pk: v for pk, v in scores.items() if evidence.get(pk, 0) > 0}
    return standardize_dict(eligible, 100.0, 10.0)


@dataclass
class RaceObj:
    race_id: str
    year: int
    race_name: str
    country: str
    track: str
    surface_raw: str
    family: str
    fold: int
    horses: List[Tuple[int, str, str]]  # placing, pk, name


# ---------------------------------------------------------------------------
# Surface-specific annual rating
# ---------------------------------------------------------------------------

def build_race_objects(df: pd.DataFrame) -> Tuple[List[RaceObj], Counter, int]:
    races: List[RaceObj] = []
    surface_counts = Counter()
    duplicate_pk_races = 0

    for row in df.itertuples(index=False):
        year = int(row.year)
        raw_surface = _txt(row.surface)
        fam = surface_family(raw_surface)
        surface_counts[(raw_surface or "<blank>", fam)] += 1

        candidates = [
            (1, normalize_pk(row.winner_pk), _txt(row.winner_name)),
            (2, normalize_pk(row.second_pk), _txt(row.second_name)),
            (3, normalize_pk(row.third_pk), _txt(row.third_name)),
        ]
        seen = set()
        horses = []
        dup = False
        for placing, pk, name in candidates:
            if not valid_pk(pk):
                continue
            if pk in seen:
                dup = True
                continue
            seen.add(pk)
            horses.append((placing, pk, name))
        if dup:
            duplicate_pk_races += 1
        if len(horses) < 2:
            continue
        races.append(RaceObj(
            race_id=_txt(row.race_id), year=year, race_name=_txt(row.race_name),
            country=normalize_country(row.country), track=_norm(row.track) or "UNKNOWN",
            surface_raw=raw_surface, family=fam, fold=stable_fold(_txt(row.race_id)),
            horses=horses,
        ))
    return races, surface_counts, duplicate_pk_races


def race_comparisons(race: RaceObj) -> List[Tuple[str, str]]:
    ordered = sorted(race.horses, key=lambda x: x[0])
    out = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            out.append((ordered[i][1], ordered[j][1]))
    return out


def fit_year_surface(year: int, family: str, races: Sequence[RaceObj]) -> Tuple[pd.DataFrame, dict]:
    all_comps: List[Tuple[str, str]] = []
    comps_by_fold: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
    names: Dict[str, str] = {}
    stats = defaultdict(lambda: {"top3": 0, "wins": 0, "seconds": 0, "thirds": 0})

    for race in races:
        comps = race_comparisons(race)
        all_comps.extend(comps)
        comps_by_fold[race.fold].extend(comps)
        for placing, pk, name in race.horses:
            if name:
                names[pk] = name
            stats[pk]["top3"] += 1
            if placing == 1:
                stats[pk]["wins"] += 1
            elif placing == 2:
                stats[pk]["seconds"] += 1
            elif placing == 3:
                stats[pk]["thirds"] += 1

    full_scores, full_ev, full_ok, full_msg = fit_bt(all_comps)
    full_rating = bt_to_rating(full_scores, full_ev)
    if not full_rating:
        return pd.DataFrame(), {
            "year": year, "surface_family": family, "races": len(races),
            "horses": 0, "comparisons": len(all_comps), "fit_success": 0,
            "fit_message": full_msg,
        }

    fold_rating: Dict[int, Dict[str, float]] = {}
    fold_ev: Dict[int, Dict[str, int]] = {}
    fold_success = 0
    for fold in range(CROSSFIT_FOLDS):
        training = []
        for other_fold, comps in comps_by_fold.items():
            if other_fold != fold:
                training.extend(comps)
        fs, fe, ok, _msg = fit_bt(training)
        fold_rating[fold] = bt_to_rating(fs, fe)
        fold_ev[fold] = fe
        fold_success += int(ok)

    contexts = defaultdict(list)  # pk -> [(race-context, reliability)]
    for race in races:
        fr = fold_rating.get(race.fold, {})
        ordered = sorted(race.horses, key=lambda x: x[0])
        for placing, pk, _name in ordered:
            opp = [fr[opk] for _, opk, _ in ordered if opk != pk and opk in fr]
            if not opp:
                continue
            rel = 1.0 if len(opp) >= 2 else 0.5
            ctx = float(np.mean(opp)) + PLACE_ADJUSTMENT.get(placing, 0.0)
            contexts[pk].append((ctx, rel))

    context_raw = {}
    context_n = {}
    avg_opp_proxy = {}
    for pk, vals in contexts.items():
        if not vals:
            continue
        ordered_vals = sorted(vals, key=lambda x: x[0], reverse=True)
        top3_mean = weighted_mean(ordered_vals[:3])
        all_mean = weighted_mean(ordered_vals)
        if top3_mean is None or all_mean is None:
            continue
        context_raw[pk] = 0.70 * top3_mean + 0.30 * all_mean
        context_n[pk] = len(vals)
        # Reliability 1.0 ~ two opponents; 0.5 ~ one opponent.
        avg_opp_proxy[pk] = float(np.mean([2.0 if w >= 0.99 else 1.0 for _, w in vals]))

    context_rating = standardize_dict(context_raw, 100.0, 10.0)

    rows = []
    prelim = {}
    for pk, bt in full_rating.items():
        st = stats[pk]
        n = max(1, st["top3"])
        ctx = context_rating.get(pk)
        gap = (ctx - bt) if ctx is not None else 0.0
        if ctx is None:
            direction_share = 0.0
            adj = 0.0
        elif gap > 0:
            direction_share = (st["seconds"] + st["thirds"]) / n
            adj = min(POSITIVE_CONTEXT_CAP, POSITIVE_CONTEXT_RATE * direction_share * gap)
        elif gap < 0:
            direction_share = st["wins"] / n
            adj = max(NEGATIVE_CONTEXT_FLOOR, NEGATIVE_CONTEXT_RATE * direction_share * gap)
        else:
            direction_share = 0.0
            adj = 0.0

        unshrunk = bt + adj
        effective_evidence = int(full_ev.get(pk, 0) + 2 * context_n.get(pk, 0))
        conf = effective_evidence / (effective_evidence + PRIOR_EVIDENCE) if effective_evidence > 0 else 0.0
        shrunk = 100.0 + conf * (unshrunk - 100.0)
        prelim[pk] = shrunk
        rows.append({
            "year": year, "surface_family": family, "horse_pk": pk,
            "horse_name": names.get(pk, pk),
            "bt_surface_rating": bt,
            "context_surface_rating": ctx,
            "context_gap": gap if ctx is not None else None,
            "context_direction_share": direction_share,
            "context_adjustment": adj,
            "unshrunk_surface_rating": unshrunk,
            "surface_comparison_evidence": int(full_ev.get(pk, 0)),
            "surface_context_appearances": int(context_n.get(pk, 0)),
            "surface_effective_evidence": effective_evidence,
            "surface_confidence": conf,
            "surface_top3_appearances": int(st["top3"]),
            "surface_wins": int(st["wins"]),
            "surface_seconds": int(st["seconds"]),
            "surface_thirds": int(st["thirds"]),
            "avg_opponents_per_context": avg_opp_proxy.get(pk),
        })

    # Final same-family recenter.  This does not change within-family ordering.
    shift = 100.0 - float(np.mean(list(prelim.values()))) if prelim else 0.0
    for row in rows:
        row["annual_surface_rating"] = prelim[row["horse_pk"]] + shift

    out = pd.DataFrame(rows)
    out["surface_rank"] = out["annual_surface_rating"].rank(method="min", ascending=False).astype(int)
    n_h = len(out)
    out["surface_percentile"] = [percentile_from_rank(int(r), n_h) for r in out["surface_rank"]]
    out = out.sort_values(["surface_rank", "horse_pk"]).reset_index(drop=True)

    return out, {
        "year": year, "surface_family": family, "races": len(races),
        "horses": n_h, "comparisons": len(all_comps),
        "fit_success": int(full_ok), "fit_message": full_msg,
        "crossfit_folds_successful": fold_success,
        "final_mean": float(out["annual_surface_rating"].mean()) if n_h else None,
        "final_sd": float(out["annual_surface_rating"].std(ddof=0)) if n_h else None,
    }


# ---------------------------------------------------------------------------
# Dynamic network partitioning inside one year x surface family
# ---------------------------------------------------------------------------

def build_pair_counts(races: Sequence[RaceObj]) -> Counter:
    pc = Counter()
    for race in races:
        pks = [pk for _, pk, _ in race.horses]
        for i in range(len(pks)):
            for j in range(i + 1, len(pks)):
                a, b = sorted((pks[i], pks[j]))
                pc[(a, b)] += 1
    return pc


def build_weighted_adj(pair_counts: Counter):
    adj = defaultdict(dict)
    raw_adj = defaultdict(dict)
    for (a, b), count in pair_counts.items():
        w = math.log1p(count)
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w
        raw_adj[a][b] = raw_adj[a].get(b, 0) + count
        raw_adj[b][a] = raw_adj[b].get(a, 0) + count
    return adj, raw_adj


def connected_components(nodes: Iterable[str], adj) -> List[List[str]]:
    unseen = set(nodes)
    comps = []
    while unseen:
        start = min(unseen)
        q = [start]
        unseen.remove(start)
        comp = []
        while q:
            u = q.pop()
            comp.append(u)
            for v in adj.get(u, {}):
                if v in unseen:
                    unseen.remove(v)
                    q.append(v)
        comps.append(sorted(comp))
    comps.sort(key=lambda c: (-len(c), c[0] if c else ""))
    return comps


def relabel_partition(part: Dict[str, int]) -> Dict[str, int]:
    groups = defaultdict(list)
    for node, cid in part.items():
        groups[cid].append(node)
    ordered = sorted(groups.values(), key=lambda xs: (-len(xs), min(xs)))
    out = {}
    for new_id, nodes in enumerate(ordered):
        for n in nodes:
            out[n] = new_id
    return out


def local_louvain(nodes: Sequence[str], adj, resolution=DYNAMIC_RESOLUTION) -> Dict[str, int]:
    part = {n: i for i, n in enumerate(sorted(nodes))}
    degree = {n: sum(adj.get(n, {}).values()) for n in nodes}
    m2 = sum(degree.values())
    if m2 <= 0:
        return {n: 0 for n in nodes}
    tot = {part[n]: degree[n] for n in nodes}

    for _ in range(DYNAMIC_NODE_SWEEPS):
        moved = 0
        for node in sorted(nodes):
            ki = degree[node]
            cur = part[node]
            neigh_w = defaultdict(float)
            for nb, w in adj.get(node, {}).items():
                if nb in part:
                    neigh_w[part[nb]] += w

            tot[cur] = tot.get(cur, 0.0) - ki
            candidates = set(neigh_w) | {cur}
            best = cur
            best_gain = neigh_w.get(cur, 0.0) - resolution * ki * tot.get(cur, 0.0) / m2
            for cid in sorted(candidates):
                gain = neigh_w.get(cid, 0.0) - resolution * ki * tot.get(cid, 0.0) / m2
                if gain > best_gain + 1e-12 or (abs(gain - best_gain) <= 1e-12 and cid < best):
                    best_gain = gain
                    best = cid
            part[node] = best
            tot[best] = tot.get(best, 0.0) + ki
            moved += int(best != cur)
        if moved == 0:
            break
    return relabel_partition(part)


def merge_positive_communities(part: Dict[str, int], adj, resolution=DYNAMIC_RESOLUTION) -> Dict[str, int]:
    part = dict(part)
    nodes = sorted(part)
    degree = {n: sum(adj.get(n, {}).values()) for n in nodes}
    m2 = sum(degree.values())
    if m2 <= 0:
        return relabel_partition(part)

    for _ in range(DYNAMIC_MERGE_ROUNDS):
        totals = defaultdict(float)
        inter = defaultdict(float)
        for n in nodes:
            totals[part[n]] += degree[n]
        seen = set()
        for a in nodes:
            ca = part[a]
            for b, w in adj.get(a, {}).items():
                if b not in part:
                    continue
                edge = tuple(sorted((a, b)))
                if edge in seen:
                    continue
                seen.add(edge)
                cb = part[b]
                if ca != cb:
                    inter[tuple(sorted((ca, cb)))] += w

        gains = []
        for (ca, cb), wab in inter.items():
            gain = wab - resolution * totals[ca] * totals[cb] / m2
            if gain > 1e-12:
                gains.append((gain, ca, cb))
        if not gains:
            break
        gains.sort(key=lambda x: (-x[0], x[1], x[2]))
        used = set()
        mapping = {}
        merged = 0
        for gain, ca, cb in gains:
            if ca in used or cb in used:
                continue
            target = min(ca, cb)
            source = max(ca, cb)
            mapping[source] = target
            used.add(ca); used.add(cb)
            merged += 1
        if merged == 0:
            break
        for n in nodes:
            c = part[n]
            part[n] = mapping.get(c, c)
        part = relabel_partition(part)
    return part


def conductance_for_comm(cid: int, part: Dict[str, int], adj) -> float:
    nodes = [n for n, c in part.items() if c == cid]
    all_nodes = list(part)
    total_vol = sum(sum(adj.get(n, {}).values()) for n in all_nodes)
    vol = sum(sum(adj.get(n, {}).values()) for n in nodes)
    cut = 0.0
    node_set = set(nodes)
    for n in nodes:
        for nb, w in adj.get(n, {}).items():
            if nb in part and nb not in node_set:
                cut += w
    denom = min(vol, total_vol - vol)
    if denom <= 1e-12:
        return 0.0
    return cut / denom


def conductance_guard(part: Dict[str, int], adj) -> Dict[str, int]:
    part = relabel_partition(part)
    for _ in range(500):
        cids = sorted(set(part.values()))
        if len(cids) <= 1:
            break
        bad = [(conductance_for_comm(cid, part, adj), cid) for cid in cids]
        bad = [(phi, cid) for phi, cid in bad if phi > DYNAMIC_MAX_ACCEPTED_CONDUCTANCE + 1e-12]
        if not bad:
            break
        phi, cid = max(bad, key=lambda x: (x[0], -x[1]))
        boundary = defaultdict(float)
        for n, c in part.items():
            if c != cid:
                continue
            for nb, w in adj.get(n, {}).items():
                if nb in part and part[nb] != cid:
                    boundary[part[nb]] += w
        if not boundary:
            break
        target = max(boundary.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        for n in list(part):
            if part[n] == cid:
                part[n] = target
        part = relabel_partition(part)
    return part


def partition_modularity(part: Dict[str, int], adj, resolution=DYNAMIC_RESOLUTION) -> float:
    nodes = list(part)
    degree = {n: sum(adj.get(n, {}).values()) for n in nodes}
    m2 = sum(degree.values())
    if m2 <= 0:
        return 0.0
    m = m2 / 2.0
    internal = defaultdict(float)
    vol = defaultdict(float)
    seen = set()
    for n in nodes:
        vol[part[n]] += degree[n]
        for nb, w in adj.get(n, {}).items():
            if nb not in part:
                continue
            edge = tuple(sorted((n, nb)))
            if edge in seen:
                continue
            seen.add(edge)
            if part[n] == part[nb]:
                internal[part[n]] += w
    q = 0.0
    for cid in set(part.values()):
        q += internal[cid] / m - resolution * (vol[cid] / m2) ** 2
    return q


def dynamic_partition(pair_counts: Counter):
    adj, raw_adj = build_weighted_adj(pair_counts)
    nodes = sorted(adj)
    parents = connected_components(nodes, adj)
    final_groups = []
    parent_map = {}
    parent_sizes = {}

    for parent_id, comp in enumerate(parents, start=1):
        for n in comp:
            parent_map[n] = parent_id
        parent_sizes[parent_id] = len(comp)
        if len(comp) < DYNAMIC_MIN_PARENT_HORSES:
            final_groups.append(comp)
            continue
        sub_adj = {n: {nb: w for nb, w in adj.get(n, {}).items() if nb in set(comp)} for n in comp}
        part = local_louvain(comp, sub_adj)
        part = merge_positive_communities(part, sub_adj)
        part = conductance_guard(part, sub_adj)
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
    return net_map, parent_map, parent_sizes, adj, raw_adj, partition_modularity(net_map, adj)


def top_counter_text(counter: Counter, limit=5) -> str:
    return " | ".join(f"{k}:{v}" for k, v in counter.most_common(limit))


def build_dynamic_outputs(year: int, family: str, races: Sequence[RaceObj], rating_df: pd.DataFrame):
    pair_counts = build_pair_counts(races)
    if not pair_counts or rating_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    net_map, parent_map, parent_sizes, adj, raw_adj, modularity = dynamic_partition(pair_counts)

    rating_by_pk = dict(zip(rating_df.horse_pk, rating_df.annual_surface_rating))
    name_by_pk = dict(zip(rating_df.horse_pk, rating_df.horse_name))

    # Appearance descriptors are post-partition explanation only.
    countries = defaultdict(Counter)
    tracks = defaultdict(Counter)
    raw_surfaces = defaultdict(Counter)
    network_races = defaultdict(set)
    japan_apps = Counter()
    horse_country_apps = defaultdict(Counter)
    horse_track_apps = defaultdict(Counter)
    horse_raw_surface_apps = defaultdict(Counter)

    for race in races:
        touched = set()
        for _placing, pk, _name in race.horses:
            nid = net_map.get(pk)
            if nid is None:
                continue
            touched.add(nid)
            countries[nid][race.country] += 1
            tracks[nid][race.track] += 1
            raw_surfaces[nid][race.surface_raw or "<blank>"] += 1
            horse_country_apps[pk][race.country] += 1
            horse_track_apps[pk][race.track] += 1
            horse_raw_surface_apps[pk][race.surface_raw or "<blank>"] += 1
            if race.country == "JAPAN":
                japan_apps[pk] += 1
        for nid in touched:
            network_races[nid].add(race.race_id)

    nodes_by_net = defaultdict(list)
    for pk, nid in net_map.items():
        nodes_by_net[nid].append(pk)

    # Raw edge metrics.
    internal_edges = Counter(); internal_comps = Counter(); external_edges = Counter(); external_comps = Counter()
    bridge_horses = defaultdict(set)
    bridge_comp_by_horse = Counter(); all_comp_by_horse = Counter(); bridge_neighbor_by_horse = defaultdict(set)
    for (a, b), count in pair_counts.items():
        na, nb = net_map[a], net_map[b]
        all_comp_by_horse[a] += count; all_comp_by_horse[b] += count
        if na == nb:
            internal_edges[na] += 1
            internal_comps[na] += count
        else:
            external_edges[na] += 1; external_edges[nb] += 1
            external_comps[na] += count; external_comps[nb] += count
            bridge_horses[na].update((a,)); bridge_horses[nb].update((b,))
            bridge_comp_by_horse[a] += count; bridge_comp_by_horse[b] += count
            bridge_neighbor_by_horse[a].add(b); bridge_neighbor_by_horse[b].add(a)

    summary_rows = []
    horse_rows = []
    largest_n = max((len(v) for v in nodes_by_net.values()), default=0)

    for nid in sorted(nodes_by_net):
        members = nodes_by_net[nid]
        member_set = set(members)
        # Conductance on the complete year-surface graph.
        part = {pk: net_map[pk] for pk in net_map}
        phi = conductance_for_comm(nid, part, adj)
        inc_raw = 2 * internal_comps[nid] + external_comps[nid]
        bridge_share = (external_comps[nid] / inc_raw) if inc_raw else 0.0
        parent_id = parent_map.get(members[0], 0)
        ratings = [(rating_by_pk.get(pk, -1e99), pk) for pk in members if pk in rating_by_pk]
        ratings.sort(reverse=True)
        top_rating, top_pk = ratings[0] if ratings else (None, "")
        dominant_country = countries[nid].most_common(1)[0][0] if countries[nid] else "UNKNOWN"
        dominant_country_share = (countries[nid][dominant_country] / sum(countries[nid].values())) if countries[nid] else None
        dom_raw_surface = raw_surfaces[nid].most_common(1)[0][0] if raw_surfaces[nid] else "UNKNOWN"
        summary_rows.append({
            "year": year, "surface_family": family, "dynamic_network_id": nid,
            "is_largest_dynamic_network": int(len(members) == largest_n),
            "parent_component_id": parent_id,
            "parent_component_horses": parent_sizes.get(parent_id, len(members)),
            "dynamic_network_horses": len(members),
            "dynamic_network_races": len(network_races[nid]),
            "internal_pair_edges": int(internal_edges[nid]),
            "internal_comparisons": int(internal_comps[nid]),
            "external_pair_edges": int(external_edges[nid]),
            "external_comparisons": int(external_comps[nid]),
            "bridge_horses": len(bridge_horses[nid]),
            "bridge_comparison_share": bridge_share,
            "conductance": phi,
            "partition_modularity": modularity,
            "dominant_country": dominant_country,
            "dominant_country_share": dominant_country_share,
            "dominant_raw_surface": dom_raw_surface,
            "top_countries": top_counter_text(countries[nid]),
            "top_tracks": top_counter_text(tracks[nid]),
            "top_raw_surfaces": top_counter_text(raw_surfaces[nid]),
            "annual_surface_rating_mean": float(np.mean([rating_by_pk[pk] for pk in members if pk in rating_by_pk])) if ratings else None,
            "annual_surface_rating_max": top_rating,
            "top_horse_pk": top_pk,
            "top_horse_name": name_by_pk.get(top_pk, top_pk),
        })

        ranked = sorted([pk for pk in members if pk in rating_by_pk], key=lambda pk: (-rating_by_pk[pk], pk))
        rank_map = {pk: i + 1 for i, pk in enumerate(ranked)}
        for pk in ranked:
            rank = rank_map[pk]
            nnet = len(ranked)
            horse_rows.append({
                "year": year, "surface_family": family, "horse_pk": pk,
                "horse_name": name_by_pk.get(pk, pk),
                "annual_surface_rating": rating_by_pk[pk],
                "dynamic_network_id": nid,
                "dynamic_network_rank": rank,
                "dynamic_network_percentile": percentile_from_rank(rank, nnet),
                "dynamic_network_horses": len(members),
                "dynamic_network_races": len(network_races[nid]),
                "parent_component_id": parent_id,
                "parent_component_horses": parent_sizes.get(parent_id, len(members)),
                "is_largest_dynamic_network": int(len(members) == largest_n),
                "bridge_neighbor_count": len(bridge_neighbor_by_horse[pk]),
                "bridge_comparisons": int(bridge_comp_by_horse[pk]),
                "bridge_comparison_share": (bridge_comp_by_horse[pk] / all_comp_by_horse[pk]) if all_comp_by_horse[pk] else 0.0,
                "network_bridge_comparison_share": bridge_share,
                "network_conductance": phi,
                "partition_modularity": modularity,
                "dominant_country": dominant_country,
                "dominant_country_share": dominant_country_share,
                "top_countries": top_counter_text(countries[nid]),
                "top_tracks": top_counter_text(tracks[nid]),
                "japan_top3_appearances": int(japan_apps[pk]),
                "horse_top_countries": top_counter_text(horse_country_apps[pk]),
                "horse_top_tracks": top_counter_text(horse_track_apps[pk]),
                "horse_raw_surfaces": top_counter_text(horse_raw_surface_apps[pk]),
            })

    return pd.DataFrame(horse_rows), pd.DataFrame(summary_rows)


# ---------------------------------------------------------------------------
# Multi-surface summaries
# ---------------------------------------------------------------------------

def effective_category_count(counter: Dict[str, int]) -> float:
    vals = np.asarray([v for v in counter.values() if v > 0], dtype=float)
    if vals.size == 0:
        return 0.0
    p = vals / vals.sum()
    h = -float(np.sum(p * np.log(p)))
    return float(math.exp(h))


def build_multisurface_summary(surface_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (year, pk), g in surface_df.groupby(["year", "horse_pk"], sort=True):
        g = g.copy()
        name = g["horse_name"].dropna().astype(str).iloc[0] if len(g) else pk
        reliable = g[
            (g["surface_comparison_evidence"] >= RELIABLE_SURFACE_COMPARISON_EVIDENCE) &
            (g["surface_top3_appearances"] >= RELIABLE_SURFACE_TOP3_APPEARANCES)
        ].copy()
        candidate = reliable if len(reliable) else g
        primary = candidate.sort_values(["annual_surface_rating", "surface_family"], ascending=[False, True]).iloc[0]
        floor = float(reliable["annual_surface_rating"].min()) if len(reliable) >= 2 else None
        surface_counts = {r.surface_family: int(r.surface_top3_appearances) for r in g.itertuples(index=False)}

        row = {
            "year": int(year), "horse_pk": pk, "horse_name": name,
            "primary_surface_family": primary["surface_family"],
            "primary_strength_relative": float(primary["annual_surface_rating"]),
            "primary_surface_comparison_evidence": int(primary["surface_comparison_evidence"]),
            "primary_surface_top3_appearances": int(primary["surface_top3_appearances"]),
            "surface_rating_count": int(len(g)),
            "reliable_surface_count": int(len(reliable)),
            "multi_surface_verified": int(len(reliable) >= 2),
            "multi_surface_floor_relative": floor,
            "primary_minus_floor": (float(primary["annual_surface_rating"]) - floor) if floor is not None else None,
            "effective_top3_surface_count": effective_category_count(surface_counts),
            "total_surface_top3_appearances": int(g["surface_top3_appearances"].sum()),
        }
        for fam in SURFACE_FAMILIES:
            gg = g[g.surface_family == fam]
            if len(gg):
                r = gg.iloc[0]
                row[f"{fam.lower()}_rating"] = float(r.annual_surface_rating)
                row[f"{fam.lower()}_rank"] = int(r.surface_rank)
                row[f"{fam.lower()}_comparison_evidence"] = int(r.surface_comparison_evidence)
                row[f"{fam.lower()}_top3_appearances"] = int(r.surface_top3_appearances)
            else:
                row[f"{fam.lower()}_rating"] = None
                row[f"{fam.lower()}_rank"] = None
                row[f"{fam.lower()}_comparison_evidence"] = 0
                row[f"{fam.lower()}_top3_appearances"] = 0
        rows.append(row)
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["year", "primary_strength_relative", "horse_pk"], ascending=[True, False, True]).reset_index(drop=True)
    return out


def build_surface_overlap_matrix(surface_df: pd.DataFrame) -> pd.DataFrame:
    reliable = surface_df[
        (surface_df.surface_comparison_evidence >= RELIABLE_SURFACE_COMPARISON_EVIDENCE) &
        (surface_df.surface_top3_appearances >= RELIABLE_SURFACE_TOP3_APPEARANCES)
    ][["year", "horse_pk", "surface_family", "annual_surface_rating"]].copy()
    rows = []
    fams = list(SURFACE_FAMILIES)
    for i, a in enumerate(fams):
        for b in fams[i + 1:]:
            ga = reliable[reliable.surface_family == a].rename(columns={"annual_surface_rating": "rating_a"})
            gb = reliable[reliable.surface_family == b].rename(columns={"annual_surface_rating": "rating_b"})
            m = ga.merge(gb, on=["year", "horse_pk"], how="inner")
            n = len(m)
            if n == 0:
                continue
            corr = float(m[["rating_a", "rating_b"]].corr().iloc[0, 1]) if n >= 3 and m.rating_a.std(ddof=0) > 0 and m.rating_b.std(ddof=0) > 0 else None
            rows.append({
                "surface_a": a, "surface_b": b, "horse_years": n,
                "mean_rating_a": float(m.rating_a.mean()),
                "mean_rating_b": float(m.rating_b.mean()),
                "mean_abs_rating_gap": float(np.mean(np.abs(m.rating_a - m.rating_b))),
                "pearson_same_horse_year": corr,
                "both_110_plus_share": float(((m.rating_a >= 110) & (m.rating_b >= 110)).mean()),
                "both_115_plus_share": float(((m.rating_a >= 115) & (m.rating_b >= 115)).mean()),
            })
    return pd.DataFrame(rows)


def build_japan_surface_ranking(surface_df: pd.DataFrame, dynamic_df: pd.DataFrame) -> pd.DataFrame:
    if dynamic_df.empty:
        return pd.DataFrame()
    cols = [
        "year", "surface_family", "horse_pk", "japan_top3_appearances",
        "dynamic_network_id", "dynamic_network_rank", "dynamic_network_percentile",
        "dynamic_network_horses", "network_conductance", "dominant_country",
    ]
    merged = surface_df.merge(dynamic_df[cols], on=["year", "surface_family", "horse_pk"], how="left")
    merged = merged[merged.japan_top3_appearances.fillna(0) > 0].copy()
    if merged.empty:
        return merged
    merged["japan_surface_rank"] = merged.groupby(["year", "surface_family"])["annual_surface_rating"].rank(method="min", ascending=False).astype(int)
    counts = merged.groupby(["year", "surface_family"])["horse_pk"].transform("count")
    merged["japan_surface_horse_count"] = counts.astype(int)
    merged["japan_surface_percentile"] = [percentile_from_rank(int(r), int(n)) for r, n in zip(merged.japan_surface_rank, counts)]
    return merged.sort_values(["year", "surface_family", "japan_surface_rank", "horse_pk"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Report / main
# ---------------------------------------------------------------------------

def write_report(path: Path, args, races_path: Path, source_rows: int, filtered_rows: int,
                 race_objs: Sequence[RaceObj], surface_counts: Counter,
                 annual_meta: List[dict], surface_df: pd.DataFrame,
                 dynamic_summary: pd.DataFrame, multi_df: pd.DataFrame,
                 overlap_df: pd.DataFrame, duplicate_pk_races: int):
    lines = []
    lines.append("グレード非参照・Multi-Surface Hard Split 実験レポート")
    lines.append(f"script: {SCRIPT_VERSION}")
    lines.append(f"input: {races_path.name}")
    lines.append(f"source rows: {source_rows:,}")
    lines.append(f"selected rows: {filtered_rows:,}")
    lines.append(f"usable Top2/Top3 races: {len(race_objs):,}")
    lines.append(f"same-race duplicate PK detected: {duplicate_pk_races:,}")
    lines.append("")
    lines.append("【Hard split】")
    lines.append("TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN をrating fit前に完全分離。")
    lines.append("grade / series_grade はusecolsに含めず、計算・network分割・順位で不使用。")
    lines.append("SYNTHETICはAll-Weather/AW/Polytrack/Tapeta/Cushion/Fibresand/Pro-Ride等を含み、DIRTへ吸収しない。")
    lines.append("")
    fam_races = Counter(r.family for r in race_objs)
    for fam in SURFACE_FAMILIES:
        lines.append(f"{fam}: usable races={fam_races.get(fam, 0):,}")
    lines.append("")
    lines.append("【Surface-specific horse model】")
    lines.append("各 year x surface family 内でL2 Bradley-Terry→race hash 5-fold cross-fit→opponent-only context→非対称補正→証拠縮小→最終平均100へ再センタリング。")
    lines.append(f"positive context: {POSITIVE_CONTEXT_RATE:.2f} x (2着+3着率) x gap, cap +{POSITIVE_CONTEXT_CAP:g}")
    lines.append(f"negative context: {NEGATIVE_CONTEXT_RATE:.2f} x 勝率 x gap, floor {NEGATIVE_CONTEXT_FLOOR:g}")
    lines.append(f"prior evidence: {PRIOR_EVIDENCE:g}")
    lines.append("")
    lines.append("【Dynamic network】")
    lines.append("surface family内だけでTop3比較graphを作成。同一pair反復はpartition時log(1+n)へ圧縮。")
    lines.append(f"parent component < {DYNAMIC_MIN_PARENT_HORSES}頭は分割しない。conductance>{DYNAMIC_MAX_ACCEPTED_CONDUCTANCE:.2f}は最強隣接communityへ再結合。")
    lines.append("country/track/raw surfaceはpartition完了後の説明列のみ。")
    lines.append("")
    lines.append("【Cross-surface interpretation】")
    lines.append("Surface ratingはsurfaceごとに平均100座標なので、異surfaceの同一点数を絶対能力として同一視しない。")
    lines.append("primary_strength_relative=max(reliable surface rating) は記述的な主surface強度。")
    lines.append("multi_surface_floor_relative=min(reliable surface ratings) は複数surfaceで弱い側まで高い馬を見るdiagnostic。")
    lines.append("一surface専念馬はfloor欠損であり減点しない。")
    lines.append("")
    lines.append("【Counts】")
    lines.append(f"surface horse-year rows: {len(surface_df):,}")
    lines.append(f"dynamic networks: {len(dynamic_summary):,}")
    lines.append(f"multi-surface horse-year rows: {len(multi_df):,}")
    if len(multi_df):
        lines.append(f"multi_surface_verified horse-years: {int(multi_df.multi_surface_verified.sum()):,}")
    lines.append("")
    lines.append("【Surface overlap diagnostic】")
    if overlap_df.empty:
        lines.append("reliableな同年複数surface馬が不足。")
    else:
        for r in overlap_df.itertuples(index=False):
            lines.append(f"{r.surface_a} x {r.surface_b}: N={r.horse_years}, mean_abs_gap={r.mean_abs_rating_gap:.3f}, corr={r.pearson_same_horse_year}")
    lines.append("")
    lines.append("注意: このv1.0はsurface hard split自体の妥当性を監査する探索版。Venue breadth / international breadthによるconfidence解除はまだratingへ入れない。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_years(text: Optional[str]) -> Optional[set]:
    if not text:
        return None
    out = set()
    for token in str(text).split(","):
        token = token.strip()
        if token:
            out.add(int(token))
    return out or None


def main():
    ap = argparse.ArgumentParser(description="Grade-free multi-surface hard-split dynamic horse rating experiment")
    ap.add_argument("--base-dir", default=str(SCRIPT_DIR), help="Folder containing stakes_races_one_row.csv")
    ap.add_argument("--races", default=None, help="Explicit stakes_races_one_row CSV")
    ap.add_argument("--output-dir", default=None, help=f"Default: <base-dir>/{DEFAULT_OUTPUT_DIR}")
    ap.add_argument("--year-start", type=int, default=None)
    ap.add_argument("--year-end", type=int, default=None)
    ap.add_argument("--years", default=None, help="Comma-separated exact years, e.g. 1999,2001,2023")
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    races_path = discover_races(base_dir, args.races)
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (base_dir / DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)

    print("=" * 78, flush=True)
    print("Grade-free Multi-Surface Hard Split v1.0", flush=True)
    print(f"input : {races_path}", flush=True)
    print(f"output: {out_dir}", flush=True)
    print("grade / series_grade: NOT READ", flush=True)
    print("=" * 78, flush=True)

    df = pd.read_csv(races_path, usecols=RACE_USECOLS, dtype=str, low_memory=False)
    source_rows = len(df)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df[df.year.notna()].copy()
    df["year"] = df["year"].astype(int)
    exact_years = parse_years(args.years)
    if exact_years is not None:
        df = df[df.year.isin(exact_years)].copy()
    else:
        if args.year_start is not None:
            df = df[df.year >= args.year_start].copy()
        if args.year_end is not None:
            df = df[df.year <= args.year_end].copy()
    filtered_rows = len(df)

    race_objs, surface_counts, duplicate_pk_races = build_race_objects(df)
    groups = defaultdict(list)
    for race in race_objs:
        groups[(race.year, race.family)].append(race)

    annual_frames = []
    annual_meta = []
    dynamic_frames = []
    dynamic_summary_frames = []

    for idx, ((year, fam), graces) in enumerate(sorted(groups.items()), start=1):
        print(f"[{idx}/{len(groups)}] {year} {fam}: races={len(graces)}", flush=True)
        rdf, meta = fit_year_surface(year, fam, graces)
        annual_meta.append(meta)
        if rdf.empty:
            continue
        annual_frames.append(rdf)
        dhorse, dsum = build_dynamic_outputs(year, fam, graces, rdf)
        if not dhorse.empty:
            dynamic_frames.append(dhorse)
        if not dsum.empty:
            dynamic_summary_frames.append(dsum)

    surface_df = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    dynamic_df = pd.concat(dynamic_frames, ignore_index=True) if dynamic_frames else pd.DataFrame()
    dynamic_summary = pd.concat(dynamic_summary_frames, ignore_index=True) if dynamic_summary_frames else pd.DataFrame()

    # Surface ranks were assigned inside each group; add a human-readable evidence flag.
    if len(surface_df):
        surface_df["surface_reliable"] = (
            (surface_df.surface_comparison_evidence >= RELIABLE_SURFACE_COMPARISON_EVIDENCE) &
            (surface_df.surface_top3_appearances >= RELIABLE_SURFACE_TOP3_APPEARANCES)
        ).astype(int)

    multi_df = build_multisurface_summary(surface_df) if len(surface_df) else pd.DataFrame()
    overlap_df = build_surface_overlap_matrix(surface_df) if len(surface_df) else pd.DataFrame()
    japan_df = build_japan_surface_ranking(surface_df, dynamic_df) if len(surface_df) and len(dynamic_df) else pd.DataFrame()

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

    if len(surface_df):
        surface_df.to_csv(outputs["surface_horse"], index=False, encoding="utf-8-sig")
    if len(dynamic_df):
        dynamic_df.to_csv(outputs["dynamic_horse"], index=False, encoding="utf-8-sig")
    if len(dynamic_summary):
        dynamic_summary.to_csv(outputs["dynamic_summary"], index=False, encoding="utf-8-sig")
    if len(multi_df):
        multi_df.to_csv(outputs["multi"], index=False, encoding="utf-8-sig")
    if len(overlap_df):
        overlap_df.to_csv(outputs["overlap"], index=False, encoding="utf-8-sig")
    if len(japan_df):
        japan_df.to_csv(outputs["japan"], index=False, encoding="utf-8-sig")
    pd.DataFrame(annual_meta).to_csv(outputs["annual_meta"], index=False, encoding="utf-8-sig")

    write_report(outputs["report"], args, races_path, source_rows, filtered_rows,
                 race_objs, surface_counts, annual_meta, surface_df,
                 dynamic_summary, multi_df, overlap_df, duplicate_pk_races)

    print("\nDone.", flush=True)
    for key, p in outputs.items():
        if p.exists():
            print(f"  {key}: {p}", flush=True)


if __name__ == "__main__":
    main()
