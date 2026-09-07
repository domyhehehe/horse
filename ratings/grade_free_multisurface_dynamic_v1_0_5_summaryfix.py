#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_free_multisurface_dynamic_v1_0_5_summaryfix.py
===================================================

Schema-safe finalizer wrapper for v1.0.4 indexed/streaming multi-surface rating.

Fixes grade_free_surface_annual_summary.csv when per-group annual_meta.csv files
have different schemas (for example, insufficient-comparison groups have only
8 fields while successful fits also have crossfit/final mean/final SD fields).

Important:
- Horse ratings / BT / context / dynamic-network logic are unchanged.
- Existing v1.0.4 checkpoints are intentionally reused.
- No --rebuild is required for this fix.
- The wrapper patches only the part-file concatenation step so every output is
  assembled with a union schema and missing values are written as blanks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import pandas as pd

import grade_free_multisurface_dynamic_v1_0_4_indexed_stream as base


WRAPPER_VERSION = "1.0.5-summary-schema-fix"


def concat_part_files_schema_safe(paths: Iterable[Path], output: Path):
    """Memory-bounded CSV assembly with a stable union of all part schemas.

    v1.0.4 assumed every part had exactly the same columns.  annual_meta parts
    legitimately differ: insufficient-comparison groups omit fields that only
    exist after a successful BT fit.  Appending such rows under the first
    file's shorter header creates malformed CSV rows.

    This function first reads only headers, creates their ordered union, then
    streams one part DataFrame at a time after reindexing to that schema.
    """
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
    readable_paths: List[Path] = []

    # Header-only pass: tiny and memory-safe even with hundreds of part files.
    for p in paths:
        try:
            cols = list(pd.read_csv(p, nrows=0).columns)
        except (pd.errors.EmptyDataError, UnicodeDecodeError):
            continue
        readable_paths.append(p)
        for col in cols:
            if col not in seen:
                seen.add(col)
                union_cols.append(col)

    if not union_cols:
        pd.DataFrame().to_csv(tmp, index=False, encoding="utf-8-sig")
        tmp.replace(output)
        return

    first = True
    for p in readable_paths:
        try:
            df = pd.read_csv(p, low_memory=False)
        except pd.errors.EmptyDataError:
            continue
        if df.empty:
            continue
        df = df.reindex(columns=union_cols)
        df.to_csv(
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


# Patch only final CSV assembly.  Keep base.SCRIPT_VERSION unchanged on purpose:
# checkpoint_manifest.json therefore remains compatible with v1.0.4 parts.
base.opt.concat_part_files = concat_part_files_schema_safe


if __name__ == "__main__":
    print(f"Multi-Surface wrapper: {WRAPPER_VERSION}", flush=True)
    print("Existing v1.0.4 checkpoints are compatible; no rebuild is needed.", flush=True)
    base.main()
