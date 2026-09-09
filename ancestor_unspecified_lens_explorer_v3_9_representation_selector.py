#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ancestor_unspecified_lens_explorer_v3_9_representation_selector.py
=================================================================

v3.8.1 branch-recovery output post-processor.

Purpose
-------
v3.8.1 successfully follows old blood-fingerprint signals toward more recent
branch representations, but some paths remain reproducible for many steps by
adding increasingly fine branches.  v3.9 does NOT change discovery or
performance testing.  It adds a practical representation-selection layer:

1. DEEPEST_REPRODUCIBLE
2. MINIMUM_SUFFICIENT
3. PRACTICAL_KNEE (main human-facing representation)
4. FIRST_BREAK
5. Pareto frontier over proximity / fidelity / complexity
6. Effective leaf count (entropy number)
7. Backward path pruning (select an earlier/simpler sufficient state)
8. Cross-source clustering of selected representations

Important separation
--------------------
- Source packages may have been selected using performance outcomes in v3.8.1.
- This selector NEVER uses performance outcome columns to choose a representation.
- Similarity / Jaccard / carrier recall / path step / branch weights only.
- Existing p/q/effect columns, when present, are merely copied as diagnostics.

This file is deliberately a companion layer rather than a fork of the large
all-in-one explorer.  It reads the v3.8.1 reconstruction-path CSV and creates
new CSV + SQLite outputs.  No famous ancestor names are hard-coded.

Standard library only.  Pyto / Windows Python compatible.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "3.9.0-selection-layer"

# ---------------------------------------------------------------------------
# Tunable policy.  These are representation-quality thresholds, NOT p-values.
# ---------------------------------------------------------------------------
SUFFICIENT_SIMILARITY = 0.95
SUFFICIENT_HIGH_JACCARD = 0.85
SUFFICIENT_CARRIER_RECALL = 0.95

# If no row reaches the stricter minimum-sufficient threshold, fall back to the
# v3.8-style generic reproducibility floor so every source still gets a result.
FALLBACK_SIMILARITY = 0.80
FALLBACK_HIGH_JACCARD = 0.65
FALLBACK_CARRIER_RECALL = 0.70

# Near-identical fidelity improvement smaller than this is treated as tiny when
# describing marginal complexity growth.  It is diagnostic only; knee selection
# itself uses geometry on the Pareto frontier rather than this constant.
TINY_FIDELITY_GAIN = 0.005

# Cross-source cluster thresholds.  Prefer an exact carrier signature when the
# path CSV happens to contain one.  Otherwise use weighted leaf-set similarity.
CLUSTER_WEIGHTED_JACCARD = 0.72
CLUSTER_PLAIN_JACCARD = 0.65

PATH_GLOB = "*_multi_blood_reconstruction_path.csv"


def _norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _first_col(fieldnames: Sequence[str], aliases: Sequence[str]) -> Optional[str]:
    by_norm = {_norm(c): c for c in fieldnames}
    for a in aliases:
        if _norm(a) in by_norm:
            return by_norm[_norm(a)]
    # conservative contains fallback
    for a in aliases:
        na = _norm(a)
        if not na:
            continue
        for c in fieldnames:
            nc = _norm(c)
            if na in nc or nc in na:
                return c
    return None


def _f(v: object) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        x = float(s)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return None


def _i(v: object) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def _clamp01(x: Optional[float]) -> float:
    if x is None or not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, float(x)))


def _median(xs: Sequence[float]) -> Optional[float]:
    vals = sorted(float(x) for x in xs if x is not None and math.isfinite(float(x)))
    if not vals:
        return None
    n = len(vals)
    m = n // 2
    if n % 2:
        return vals[m]
    return (vals[m - 1] + vals[m]) / 2.0


def _basename_without_suffix(path: Path) -> str:
    suffix = "_multi_blood_reconstruction_path.csv"
    n = path.name
    return n[:-len(suffix)] if n.endswith(suffix) else path.stem


# ---------------------------------------------------------------------------
# Representation parsing
# ---------------------------------------------------------------------------
# v3.8.x human-readable strings can vary slightly.  We deliberately support
# several weight notations: NAME@0.25, NAME:0.25, NAME*0.25, NAME(0.25).
_WEIGHT_PATTERNS = [
    re.compile(r"([^|{},;\[\]]+?)\s*@\s*([0-9.eE+-]+)"),
    re.compile(r"([^|{},;\[\]]+?)\s*\*\s*([0-9.eE+-]+)"),
    re.compile(r"([^|{},;\[\]]+?)\s*:\s*([0-9.eE+-]+)"),
    re.compile(r"([^|{},;\[\]]+?)\s*\(\s*([0-9.eE+-]+)\s*\)"),
]


def _clean_leaf_name(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^[=><\-+\s]+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_leaf_weights(text: object) -> List[Tuple[str, float]]:
    s = str(text or "").strip()
    if not s:
        return []
    found: List[Tuple[str, float]] = []
    occupied: List[Tuple[int, int]] = []
    for pat in _WEIGHT_PATTERNS:
        for m in pat.finditer(s):
            span = m.span()
            if any(not (span[1] <= a or span[0] >= b) for a, b in occupied):
                continue
            name = _clean_leaf_name(m.group(1))
            w = _f(m.group(2))
            if name and w is not None and w > 0:
                found.append((name, float(w)))
                occupied.append(span)
    if found:
        # merge duplicate visible leaves
        d: Dict[str, float] = defaultdict(float)
        for n, w in found:
            d[n] += w
        return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))

    # No explicit weights: parse likely leaf labels and assign equal weights.
    # Ignore arrow/source labels before => when braces are present.
    body = s
    body = re.sub(r"[^|;]*=>", "", body)
    toks = [
        _clean_leaf_name(x)
        for x in re.split(r"[|,;{}\[\]]+", body)
        if _clean_leaf_name(x)
    ]
    # Filter obvious structural words / pure numerics.
    bad = {"source", "reproduced", "break", "candidate", "none", "na"}
    toks = [x for x in toks if _norm(x) not in bad and not re.fullmatch(r"[0-9.eE+-]+", x)]
    uniq: List[str] = []
    seen = set()
    for x in toks:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    if not uniq:
        return []
    w = 1.0 / len(uniq)
    return [(x, w) for x in uniq]


def effective_leaf_count(leaves: Sequence[Tuple[str, float]]) -> Optional[float]:
    ws = [abs(float(w)) for _, w in leaves if w is not None and float(w) > 0]
    total = sum(ws)
    if total <= 0:
        return None
    ps = [w / total for w in ws]
    h = -sum(p * math.log(p) for p in ps if p > 0)
    return math.exp(h)


def leaf_weight_map(text: object) -> Dict[str, float]:
    leaves = parse_leaf_weights(text)
    d: Dict[str, float] = defaultdict(float)
    for n, w in leaves:
        d[_norm(n)] += abs(float(w))
    total = sum(d.values())
    if total > 0:
        for k in list(d):
            d[k] /= total
    return dict(d)


def weighted_jaccard(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    num = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    den = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return num / den if den > 0 else 0.0


def plain_jaccard(a: Dict[str, float], b: Dict[str, float]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# Column discovery
# ---------------------------------------------------------------------------
ALIASES = {
    "source": [
        "source_package", "source_package_name", "source_package_key",
        "source_ancestors", "source", "original_package",
    ],
    "source_rank": ["source_rank", "multi_blood_rank", "package_rank", "rank"],
    "step": ["step", "path_step", "reconstruction_step", "forward_step"],
    "state": ["path_state", "state", "status", "representation_status", "stage"],
    "representation": [
        "representation", "representation_text", "current_representation",
        "reconstructed_package", "branch_representation", "package_representation",
        "nearest_reproducible_package",
    ],
    "similarity": ["similarity", "fingerprint_similarity", "representation_similarity"],
    "high_jaccard": ["high_jaccard", "high_group_jaccard", "highdose_jaccard"],
    "low_jaccard": ["low_jaccard", "low_group_jaccard", "lowdose_jaccard"],
    "carrier_recall": ["carrier_recall", "joint_carrier_recall"],
    "joint_jaccard": ["joint_carrier_jaccard", "carrier_jaccard", "joint_jaccard"],
    "high_recall": ["high_recall", "high_group_recall"],
    "log_corr": ["log_dose_corr", "logdosecorr", "dose_corr", "log_dose_pearson"],
    "leaf_count": ["leaf_count", "total_leaves", "branch_leaf_count", "n_leaves"],
    "median_year": [
        "representation_median_year", "median_year", "leaf_median_year",
        "component_median_year", "nearest_median_year",
    ],
    "carrier_signature": [
        "carrier_signature", "joint_carrier_signature", "carrier_bitset_signature",
        "target_carrier_signature",
    ],
}


def discover_columns(fieldnames: Sequence[str]) -> Dict[str, Optional[str]]:
    return {k: _first_col(fieldnames, v) for k, v in ALIASES.items()}


def read_path_csv(path: Path) -> Tuple[List[Dict[str, str]], List[str], Dict[str, Optional[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        rows = [dict(x) for x in r]
        fields = list(r.fieldnames or [])
    cols = discover_columns(fields)
    if not cols["step"] or not cols["representation"] or not cols["similarity"]:
        raise RuntimeError(
            "reconstruction path CSV columns could not be identified. "
            f"step={cols['step']} representation={cols['representation']} similarity={cols['similarity']}"
        )
    return rows, fields, cols


def source_key(row: Dict[str, str], cols: Dict[str, Optional[str]]) -> str:
    c = cols.get("source")
    if c and str(row.get(c, "")).strip():
        return str(row.get(c, "")).strip()
    sr = cols.get("source_rank")
    if sr and str(row.get(sr, "")).strip():
        return "SOURCE_RANK_" + str(row.get(sr, "")).strip()
    # Last-resort: source step representation becomes group key.
    st = _i(row.get(cols["step"])) if cols.get("step") else None
    rep = str(row.get(cols["representation"], ""))
    return ("SOURCE_REP_" + rep) if st == 0 else "UNKNOWN_SOURCE"


def row_metrics(row: Dict[str, str], cols: Dict[str, Optional[str]]) -> Dict[str, object]:
    rep = str(row.get(cols["representation"], "")) if cols.get("representation") else ""
    leaves = parse_leaf_weights(rep)
    raw_leaf = _i(row.get(cols["leaf_count"])) if cols.get("leaf_count") else None
    if raw_leaf is None:
        raw_leaf = len(leaves) if leaves else None
    eff = effective_leaf_count(leaves)
    return {
        "step": _i(row.get(cols["step"])) if cols.get("step") else None,
        "state": str(row.get(cols["state"], "")) if cols.get("state") else "",
        "representation": rep,
        "similarity": _f(row.get(cols["similarity"])) if cols.get("similarity") else None,
        "high_jaccard": _f(row.get(cols["high_jaccard"])) if cols.get("high_jaccard") else None,
        "low_jaccard": _f(row.get(cols["low_jaccard"])) if cols.get("low_jaccard") else None,
        "carrier_recall": _f(row.get(cols["carrier_recall"])) if cols.get("carrier_recall") else None,
        "joint_jaccard": _f(row.get(cols["joint_jaccard"])) if cols.get("joint_jaccard") else None,
        "high_recall": _f(row.get(cols["high_recall"])) if cols.get("high_recall") else None,
        "log_corr": _f(row.get(cols["log_corr"])) if cols.get("log_corr") else None,
        "raw_leaf_count": raw_leaf,
        "effective_leaf_count": eff if eff is not None else (float(raw_leaf) if raw_leaf is not None else None),
        "median_year": _f(row.get(cols["median_year"])) if cols.get("median_year") else None,
        "carrier_signature": str(row.get(cols["carrier_signature"], "")).strip() if cols.get("carrier_signature") else "",
        "leaf_map": leaf_weight_map(rep),
    }


def _state_is_break(s: object) -> bool:
    x = str(s or "").upper()
    return "BREAK" in x or "FAIL" in x


def _state_is_source(s: object, step: Optional[int]) -> bool:
    x = str(s or "").upper()
    return step == 0 or "SOURCE" in x


def _passes(m: Dict[str, object], sim: float, hj: float, cr: float) -> bool:
    s = m.get("similarity")
    h = m.get("high_jaccard")
    c = m.get("carrier_recall")
    # Missing secondary metrics should not silently fail old CSV formats; require
    # them only when the column actually has a value.
    if s is None or float(s) < sim:
        return False
    if h is not None and float(h) < hj:
        return False
    if c is not None and float(c) < cr:
        return False
    return True


# ---------------------------------------------------------------------------
# Pareto / knee
# ---------------------------------------------------------------------------
def dominates(a: Dict[str, object], b: Dict[str, object]) -> bool:
    """a dominates b if at least as near, faithful, simple, and strict in one."""
    astep = float(a.get("step") or 0)
    bstep = float(b.get("step") or 0)
    asim = float(a.get("similarity") or 0)
    bsim = float(b.get("similarity") or 0)
    aeff = float(a.get("effective_leaf_count") or 1e9)
    beff = float(b.get("effective_leaf_count") or 1e9)
    weak = astep >= bstep and asim >= bsim and aeff <= beff
    strict = astep > bstep or asim > bsim or aeff < beff
    return weak and strict


def pareto_frontier(ms: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    out = []
    for i, b in enumerate(ms):
        if any(i != j and dominates(a, b) for j, a in enumerate(ms)):
            continue
        out.append(b)
    return sorted(out, key=lambda m: (int(m.get("step") or 0), float(m.get("effective_leaf_count") or 1e9)))


def _norm_values(vals: Sequence[float], reverse: bool = False) -> List[float]:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        z = [0.5 for _ in vals]
    else:
        z = [(v - lo) / (hi - lo) for v in vals]
    return [1.0 - x for x in z] if reverse else z


def point_line_distance_3d(p, a, b) -> float:
    # distance from p to infinite line a-b; all coordinates already normalized.
    vx, vy, vz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    wx, wy, wz = p[0] - a[0], p[1] - a[1], p[2] - a[2]
    vv = vx * vx + vy * vy + vz * vz
    if vv <= 1e-15:
        return math.sqrt(wx * wx + wy * wy + wz * wz)
    t = (wx * vx + wy * vy + wz * vz) / vv
    qx, qy, qz = a[0] + t * vx, a[1] + t * vy, a[2] + t * vz
    dx, dy, dz = p[0] - qx, p[1] - qy, p[2] - qz
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def choose_knee(front: Sequence[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if not front:
        return None
    if len(front) <= 2:
        # With no curvature information, prefer the nearer point while still
        # retaining fidelity; this only happens on tiny paths.
        return max(front, key=lambda m: (float(m.get("step") or 0), float(m.get("similarity") or 0)))

    steps = [float(m.get("step") or 0) for m in front]
    sims = [float(m.get("similarity") or 0) for m in front]
    effs = [float(m.get("effective_leaf_count") or 0) for m in front]
    x = _norm_values(steps, reverse=False)       # proximity: higher is better
    y = _norm_values(sims, reverse=False)        # fidelity: higher is better
    z = _norm_values(effs, reverse=True)         # simplicity: higher is better
    pts = list(zip(x, y, z))
    a, b = pts[0], pts[-1]
    ds = [point_line_distance_3d(p, a, b) for p in pts]
    # Prefer interior curvature.  If all distances are numerically zero, choose
    # the frontier point nearest to the ideal corner (1,1,1).
    inner = list(range(1, len(front) - 1))
    if inner and max(ds[i] for i in inner) > 1e-12:
        idx = max(inner, key=lambda i: (ds[i], x[i] + y[i] + z[i]))
    else:
        idx = min(range(len(front)), key=lambda i: math.sqrt((1-x[i])**2 + (1-y[i])**2 + (1-z[i])**2))
    r = dict(front[idx])
    r["knee_distance"] = ds[idx]
    return r


# ---------------------------------------------------------------------------
# Per-source selection
# ---------------------------------------------------------------------------
def choose_representations(source: str, rows: Sequence[Dict[str, str]], cols: Dict[str, Optional[str]]):
    enriched = []
    for raw in rows:
        m = row_metrics(raw, cols)
        m["raw"] = raw
        m["source"] = source
        enriched.append(m)
    enriched.sort(key=lambda m: (int(m.get("step") or 0), str(m.get("state") or "")))

    valid = [m for m in enriched if not _state_is_break(m.get("state")) and m.get("similarity") is not None]
    post_source = [m for m in valid if not _state_is_source(m.get("state"), m.get("step"))]
    breaks = [m for m in enriched if _state_is_break(m.get("state"))]

    deepest = max(valid, key=lambda m: int(m.get("step") or 0)) if valid else None
    first_break = min(breaks, key=lambda m: int(m.get("step") or 10**9)) if breaks else None

    sufficient = [
        m for m in post_source
        if _passes(m, SUFFICIENT_SIMILARITY, SUFFICIENT_HIGH_JACCARD, SUFFICIENT_CARRIER_RECALL)
    ]
    sufficient_policy = "STRICT"
    if not sufficient:
        sufficient = [
            m for m in post_source
            if _passes(m, FALLBACK_SIMILARITY, FALLBACK_HIGH_JACCARD, FALLBACK_CARRIER_RECALL)
        ]
        sufficient_policy = "FALLBACK_REPRODUCIBLE"
    if not sufficient and post_source:
        sufficient = list(post_source)
        sufficient_policy = "BEST_AVAILABLE"

    # Backward path pruning: from the deepest path, choose the simplest state
    # that still passes the sufficient fingerprint threshold.  Source itself is
    # deliberately excluded so ancient labels cannot trivially win.
    minimum = None
    if sufficient:
        minimum = min(
            sufficient,
            key=lambda m: (
                float(m.get("effective_leaf_count") or 1e9),
                -(int(m.get("step") or 0)),
                -float(m.get("similarity") or 0),
            ),
        )

    # Pareto + geometric knee only among reproducible post-source states.  This
    # avoids a BREAK candidate becoming attractive merely because it is newer.
    reproducible = [
        m for m in post_source
        if _passes(m, FALLBACK_SIMILARITY, FALLBACK_HIGH_JACCARD, FALLBACK_CARRIER_RECALL)
    ]
    front = pareto_frontier(reproducible)
    knee = choose_knee(front)

    # If the knee is below the strict sufficient layer while a strict state
    # exists, choose the nearest strict Pareto state as human-facing fallback.
    if knee is not None and sufficient_policy == "STRICT" and not _passes(
        knee, SUFFICIENT_SIMILARITY, SUFFICIENT_HIGH_JACCARD, SUFFICIENT_CARRIER_RECALL
    ):
        strict_front = [m for m in front if _passes(m, SUFFICIENT_SIMILARITY, SUFFICIENT_HIGH_JACCARD, SUFFICIENT_CARRIER_RECALL)]
        if strict_front:
            knee = choose_knee(strict_front) or max(strict_front, key=lambda m: int(m.get("step") or 0))

    return {
        "source": source,
        "path": enriched,
        "deepest": deepest,
        "minimum": minimum,
        "knee": knee,
        "first_break": first_break,
        "frontier": front,
        "sufficient_policy": sufficient_policy,
    }


def _fmt(x: object, nd: int = 6) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def selected_row(source: str, label: str, m: Optional[Dict[str, object]], policy: str) -> Dict[str, object]:
    if m is None:
        return {
            "source_package": source,
            "selection_label": label,
            "selection_policy": policy,
            "status": "NOT_AVAILABLE",
        }
    return {
        "source_package": source,
        "selection_label": label,
        "selection_policy": policy,
        "status": "OK",
        "step": m.get("step"),
        "path_state": m.get("state"),
        "representation": m.get("representation"),
        "similarity": m.get("similarity"),
        "high_jaccard": m.get("high_jaccard"),
        "low_jaccard": m.get("low_jaccard"),
        "carrier_recall": m.get("carrier_recall"),
        "joint_carrier_jaccard": m.get("joint_jaccard"),
        "high_recall": m.get("high_recall"),
        "log_dose_corr": m.get("log_corr"),
        "raw_leaf_count": m.get("raw_leaf_count"),
        "effective_leaf_count": m.get("effective_leaf_count"),
        "representation_median_year": m.get("median_year"),
        "knee_distance": m.get("knee_distance"),
        "source_selection_uses_outcomes": 1,
        "representation_selection_uses_outcomes": 0,
    }


# ---------------------------------------------------------------------------
# Cross-source clustering
# ---------------------------------------------------------------------------
class DSU:
    def __init__(self, n: int):
        self.p = list(range(n))
    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def cluster_selected(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    main = [r for r in rows if r.get("selection_label") == "PRACTICAL_KNEE" and r.get("status") == "OK"]
    n = len(main)
    dsu = DSU(n)
    maps = [leaf_weight_map(r.get("representation")) for r in main]
    for i in range(n):
        for j in range(i + 1, n):
            wi = weighted_jaccard(maps[i], maps[j])
            pi = plain_jaccard(maps[i], maps[j])
            if wi >= CLUSTER_WEIGHTED_JACCARD or (wi >= 0.55 and pi >= CLUSTER_PLAIN_JACCARD):
                dsu.union(i, j)
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[dsu.find(i)].append(i)

    out = []
    ordered = sorted(groups.values(), key=lambda idxs: (-len(idxs), min(idxs)))
    for cid, idxs in enumerate(ordered, 1):
        reps = [main[i] for i in idxs]
        # Medoid by average weighted leaf similarity, outcome-independent.
        best_i = idxs[0]
        best_score = -1.0
        for i in idxs:
            sims = [weighted_jaccard(maps[i], maps[j]) for j in idxs if j != i]
            sc = sum(sims) / len(sims) if sims else 1.0
            if sc > best_score:
                best_score, best_i = sc, i
        medoid = main[best_i]
        for i in idxs:
            out.append({
                "cluster_id": cid,
                "cluster_size": len(idxs),
                "source_package": main[i].get("source_package"),
                "representation": main[i].get("representation"),
                "medoid_source_package": medoid.get("source_package"),
                "medoid_representation": medoid.get("representation"),
                "weighted_leaf_jaccard_to_medoid": weighted_jaccard(maps[i], maps[best_i]),
                "plain_leaf_jaccard_to_medoid": plain_jaccard(maps[i], maps[best_i]),
                "cluster_method": "WEIGHTED_LEAF_JACCARD_FALLBACK",
                "selection_uses_outcomes": 0,
            })
    return out


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def write_csv(path: Path, rows: Sequence[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    if not fields:
        fields = ["status"]
        rows = [{"status": "NO_ROWS"}]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k)) for k in fields})


def write_sqlite(path: Path, selected, frontier, clusters) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        def put(table: str, rows: Sequence[Dict[str, object]]):
            if not rows:
                return
            fields = []
            seen = set()
            for r in rows:
                for k in r:
                    if k not in seen:
                        seen.add(k); fields.append(k)
            qfields = ",".join('"' + x.replace('"','""') + '" TEXT' for x in fields)
            con.execute(f'CREATE TABLE "{table}" ({qfields})')
            qs = ",".join("?" for _ in fields)
            names = ",".join('"' + x.replace('"','""') + '"' for x in fields)
            con.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({qs})',
                [[_fmt(r.get(k)) for k in fields] for r in rows],
            )
        put("representation_selection", selected)
        put("pareto_frontier", frontier)
        put("representation_clusters", clusters)
        con.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        meta = {
            "version": VERSION,
            "source_selection_uses_outcomes": "1",
            "representation_selection_uses_outcomes": "0",
            "sufficient_similarity": str(SUFFICIENT_SIMILARITY),
            "sufficient_high_jaccard": str(SUFFICIENT_HIGH_JACCARD),
            "sufficient_carrier_recall": str(SUFFICIENT_CARRIER_RECALL),
            "cluster_method": "WEIGHTED_LEAF_JACCARD_FALLBACK",
        }
        con.executemany('INSERT INTO metadata(key,value) VALUES (?,?)', list(meta.items()))
        con.commit()
    finally:
        con.close()


def write_summary(path: Path, source_csv: Path, selections, frontiers, clusters) -> None:
    by_source = defaultdict(dict)
    for r in selections:
        by_source[str(r.get("source_package"))][str(r.get("selection_label"))] = r
    cluster_sizes = defaultdict(int)
    for r in clusters:
        cluster_sizes[int(r.get("cluster_id") or 0)] += 1
    with path.open("w", encoding="utf-8") as f:
        f.write(f"MULTI BLOOD REPRESENTATION SELECTOR v{VERSION}\n")
        f.write(f"source_path_csv={source_csv.name}\n")
        f.write("source_selection_uses_outcomes=1\n")
        f.write("representation_selection_uses_outcomes=0\n")
        f.write("selection_axes=PEDIGREE/BLOOD FINGERPRINT ONLY\n")
        f.write(
            "strict_sufficient="
            f"similarity>={SUFFICIENT_SIMILARITY}, "
            f"high_jaccard>={SUFFICIENT_HIGH_JACCARD}, "
            f"carrier_recall>={SUFFICIENT_CARRIER_RECALL}\n"
        )
        f.write("\n[REPRESENTATION_SELECTION]\n")
        for src in sorted(by_source):
            d = by_source[src]
            f.write(f"\nSOURCE {src}\n")
            for lab in ("DEEPEST_REPRODUCIBLE", "MINIMUM_SUFFICIENT", "PRACTICAL_KNEE", "FIRST_BREAK"):
                r = d.get(lab)
                if not r:
                    continue
                f.write(
                    f"  {lab}: step={r.get('step','')} sim={r.get('similarity','')} "
                    f"highJ={r.get('high_jaccard','')} recall={r.get('carrier_recall','')} "
                    f"leaves={r.get('raw_leaf_count','')} effLeaves={r.get('effective_leaf_count','')}\n"
                )
                f.write(f"    {r.get('representation','')}\n")
        f.write("\n[CLUSTERS]\n")
        if not clusters:
            f.write("  none\n")
        else:
            for cid in sorted(cluster_sizes):
                members = [r for r in clusters if int(r.get("cluster_id") or 0) == cid]
                med = members[0]
                f.write(
                    f"  cluster#{cid} n={len(members)} medoidSource={med.get('medoid_source_package','')}\n"
                )
                f.write(f"    medoid={med.get('medoid_representation','')}\n")
                f.write("    sources=" + " | ".join(str(r.get("source_package")) for r in members) + "\n")
        f.write("\n[INTERPRETATION]\n")
        f.write("- DEEPEST_REPRODUCIBLE is the farthest branch recovery reached, not automatically the best human explanation.\n")
        f.write("- MINIMUM_SUFFICIENT is the simplest post-source state retaining a strict blood fingerprint when possible.\n")
        f.write("- PRACTICAL_KNEE is selected from the Pareto frontier by geometric curvature in proximity/fidelity/simplicity space.\n")
        f.write("- FIRST_BREAK is the first recorded failed/break candidate when available.\n")
        f.write("- Clusters use representation-leaf similarity because carrier bitsets are not guaranteed in compact path CSV.\n")
        f.write("- No performance outcome is used to choose MINIMUM_SUFFICIENT, PRACTICAL_KNEE, or clusters.\n")


def process_one(path: Path) -> Dict[str, Path]:
    rows, fields, cols = read_path_csv(path)
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        groups[source_key(r, cols)].append(r)

    selected: List[Dict[str, object]] = []
    frontier_rows: List[Dict[str, object]] = []
    for src, rs in groups.items():
        res = choose_representations(src, rs, cols)
        selected.append(selected_row(src, "DEEPEST_REPRODUCIBLE", res["deepest"], "DEEPEST_PATH_STATE"))
        selected.append(selected_row(src, "MINIMUM_SUFFICIENT", res["minimum"], "BACKWARD_PATH_PRUNE_" + res["sufficient_policy"]))
        selected.append(selected_row(src, "PRACTICAL_KNEE", res["knee"], "PARETO_3D_GEOMETRIC_KNEE"))
        selected.append(selected_row(src, "FIRST_BREAK", res["first_break"], "FIRST_RECORDED_BREAK"))
        for m in res["frontier"]:
            frontier_rows.append({
                "source_package": src,
                "step": m.get("step"),
                "path_state": m.get("state"),
                "representation": m.get("representation"),
                "similarity": m.get("similarity"),
                "high_jaccard": m.get("high_jaccard"),
                "carrier_recall": m.get("carrier_recall"),
                "raw_leaf_count": m.get("raw_leaf_count"),
                "effective_leaf_count": m.get("effective_leaf_count"),
                "representation_median_year": m.get("median_year"),
                "pareto": 1,
                "selection_uses_outcomes": 0,
            })

    clusters = cluster_selected(selected)
    stem = _basename_without_suffix(path)
    out_dir = path.parent
    selected_path = out_dir / f"{stem}_multi_blood_practical_representations_v3_9.csv"
    frontier_path = out_dir / f"{stem}_multi_blood_pareto_frontier_v3_9.csv"
    cluster_path = out_dir / f"{stem}_multi_blood_representation_clusters_v3_9.csv"
    sqlite_path = out_dir / f"{stem}_multi_blood_representation_selection_v3_9.sqlite3"
    summary_path = out_dir / f"{stem}_multi_blood_representation_selection_v3_9.txt"
    write_csv(selected_path, selected)
    write_csv(frontier_path, frontier_rows)
    write_csv(cluster_path, clusters)
    write_sqlite(sqlite_path, selected, frontier_rows, clusters)
    write_summary(summary_path, path, selected, frontier_rows, clusters)
    return {
        "selected": selected_path,
        "frontier": frontier_path,
        "clusters": cluster_path,
        "sqlite": sqlite_path,
        "summary": summary_path,
    }


def find_inputs(root: Path) -> List[Path]:
    paths = [Path(x) for x in glob.glob(str(root / "**" / PATH_GLOB), recursive=True)]
    # Ignore v3.9 outputs if rerun from a messy directory.
    paths = [p for p in paths if "_v3_9" not in p.name]
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def self_test() -> None:
    # Simple synthetic path: a 2-leaf ancient source, then progressively newer
    # states.  5 effective leaves keep ~98%, while 9 leaves add very little.
    fieldnames = [
        "source_package", "step", "path_state", "representation",
        "similarity", "high_jaccard", "carrier_recall", "total_leaves",
    ]
    cols = discover_columns(fieldnames)
    rows = [
        {"source_package":"A|B","step":"0","path_state":"SOURCE","representation":"A@0.5|B@0.5","similarity":"1","high_jaccard":"1","carrier_recall":"1","total_leaves":"2"},
        {"source_package":"A|B","step":"4","path_state":"REPRODUCED","representation":"C@0.50|D@0.25|E@0.25","similarity":"0.970","high_jaccard":"0.90","carrier_recall":"0.97","total_leaves":"3"},
        {"source_package":"A|B","step":"8","path_state":"REPRODUCED","representation":"C@0.35|D@0.25|E@0.20|F@0.12|G@0.08","similarity":"0.983","high_jaccard":"0.957","carrier_recall":"1.0","total_leaves":"5"},
        {"source_package":"A|B","step":"16","path_state":"REPRODUCED","representation":"C@0.25|D@0.20|E@0.15|F@0.12|G@0.10|H@0.07|I@0.05|J@0.04|K@0.02","similarity":"0.999","high_jaccard":"1","carrier_recall":"1","total_leaves":"9"},
        {"source_package":"A|B","step":"17","path_state":"BREAK_CANDIDATE","representation":"X@0.5|Y@0.5","similarity":"0.70","high_jaccard":"0.55","carrier_recall":"0.65","total_leaves":"2"},
    ]
    r = choose_representations("A|B", rows, cols)
    assert r["deepest"] and r["deepest"]["step"] == 16
    assert r["minimum"] is not None
    assert r["knee"] is not None
    assert r["first_break"] and r["first_break"]["step"] == 17
    eff = effective_leaf_count(parse_leaf_weights("A@0.5|B@0.25|C@0.25"))
    assert eff is not None and 2.7 < eff < 2.9
    print("self-test: OK")
    print("minimum:", r["minimum"]["step"], r["minimum"]["representation"])
    print("knee:", r["knee"]["step"], r["knee"]["representation"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v3.8.1 multi-blood representation practical selector")
    ap.add_argument("--root", default=".", help="search root for *_multi_blood_reconstruction_path.csv")
    ap.add_argument("--input", default="", help="explicit reconstruction path CSV")
    ap.add_argument("--all", action="store_true", help="process every found path CSV instead of newest only")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        self_test()
        return 0

    if args.input:
        inputs = [Path(args.input).expanduser().resolve()]
    else:
        inputs = find_inputs(Path(args.root).expanduser().resolve())
        if not args.all and inputs:
            inputs = inputs[:1]
    if not inputs:
        print(f"ERROR: no {PATH_GLOB} found", file=sys.stderr)
        return 2

    print(f"v{VERSION}")
    for p in inputs:
        print("input:", p)
        outs = process_one(p)
        for k, v in outs.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
