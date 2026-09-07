#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GBS v2.3.1 CACHE + SQLITE + CUI hotfix
======================================

Fixes v2.3 SQLite import bug:
  table horses has 28 columns but 27 values were supplied

This file loads the v2.3 unified entry point beside it, replaces only the
horses CSV -> SQLite importer with a 28-column explicit INSERT, bumps the
schema version, then runs the normal v2.3 main flow.

Keep this file beside:
  horse_breeding_resource_gbs_v2_3_CACHE_CUI_unified.py

The GBS/rating definitions are unchanged.
"""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


HERE = Path(__file__).resolve().parent
BASE_FILE = HERE / "horse_breeding_resource_gbs_v2_3_CACHE_CUI_unified.py"

if not BASE_FILE.exists():
    raise FileNotFoundError(f"base v2.3 file not found: {BASE_FILE}")

spec = importlib.util.spec_from_file_location("gbs_v23_base", BASE_FILE)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load: {BASE_FILE}")

mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Force rebuild if an older schema-3 DB somehow exists.
mod.SCRIPT_VERSION = "2.3.1-cache-cui-hotfix"
mod.SCHEMA_VERSION = "4"


def fixed_import_horses(conn, path):
    print(f"SQLite horses取込: {path}", flush=True)

    with mod.open_csv(path) as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []

        A = {
            "pk": ["horse_pk", "PrimaryKey", "pk"],
            "name": ["horse_name", "Horse Name", "name"],
            "birth": ["birth_year", "birth", "Year"],
            "country": ["country", "Country"],
            "sire": ["sire", "sire_pk", "Sire"],
            "dam": ["dam", "dam_pk", "Dam"],
            "oraw": ["old_gbs_raw"],
            "opct": ["era_old_gbs_pct"],
            "oz": ["era_old_gbs_z"],
            "draw": ["dn_gbs_raw"],
            "dpct": ["era_dn_gbs_pct"],
            "dz": ["era_dn_gbs_z"],
            "gap": ["era_dn_minus_old_pct"],
            "orn": ["old_relative_rated_n"],
            "drn": ["dn_relative_rated_n"],
            "ocov": ["old_relative_coverage"],
            "dcov": ["dn_relative_coverage"],
            "peak": ["own_peak_annual_horse_rating"],
            "pyear": ["own_peak_rating_year"],
            "prank": ["own_peak_annual_global_rank"],
            "ppct": ["own_peak_annual_global_percentile"],
            "pconf": ["own_peak_horse_confidence"],
            "drank": ["own_peak_dynamic_network_rank"],
            "dnpct": ["own_peak_dynamic_network_percentile"],
            "dnh": ["own_peak_dynamic_network_horses"],
            "dnc": ["own_peak_dynamic_dominant_country"],
            "dns": ["own_peak_dynamic_dominant_surface"],
        }

        cols = {
            k: mod.find_col(fields, v, required=(k == "pk"))
            for k, v in A.items()
        }

        def rv(row, k):
            c = cols.get(k)
            return row.get(c, "") if c else ""

        # Explicit column list + exactly 28 placeholders.
        sql = """
        INSERT OR REPLACE INTO horses (
            horse_pk,
            horse_name,
            name_search,
            birth_year,
            country,
            sire_pk,
            dam_pk,
            old_gbs_raw,
            old_gbs_pct,
            old_gbs_z,
            dn_gbs_raw,
            dn_gbs_pct,
            dn_gbs_z,
            dn_minus_old_pct,
            old_relative_rated_n,
            dn_relative_rated_n,
            old_relative_coverage,
            dn_relative_coverage,
            peak_rating,
            peak_rating_year,
            peak_global_rank,
            peak_global_pct,
            peak_confidence,
            peak_dynamic_rank,
            peak_dynamic_pct,
            peak_dynamic_horses,
            peak_dynamic_country,
            peak_dynamic_surface
        ) VALUES (
            ?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?
        )
        """

        # Safety check: catches this class of bug before processing 500k rows.
        if sql.count("?") != 28:
            raise RuntimeError(
                f"internal SQL placeholder mismatch: expected 28, got {sql.count('?')}"
            )

        batch = []
        n = 0

        for row in rd:
            pk = mod.text(rv(row, "pk"))
            if not pk:
                continue

            name = mod.text(rv(row, "name")) or pk

            values = (
                pk,
                name,
                mod.search_norm(name),
                mod.to_int(rv(row, "birth")),
                mod.text(rv(row, "country")),
                mod.text(rv(row, "sire")),
                mod.text(rv(row, "dam")),
                mod.to_float(rv(row, "oraw")),
                mod.to_float(rv(row, "opct")),
                mod.to_float(rv(row, "oz")),
                mod.to_float(rv(row, "draw")),
                mod.to_float(rv(row, "dpct")),
                mod.to_float(rv(row, "dz")),
                mod.to_float(rv(row, "gap")),
                mod.to_int(rv(row, "orn")),
                mod.to_int(rv(row, "drn")),
                mod.to_float(rv(row, "ocov")),
                mod.to_float(rv(row, "dcov")),
                mod.to_float(rv(row, "peak")),
                mod.to_int(rv(row, "pyear")),
                mod.to_int(rv(row, "prank")),
                mod.to_float(rv(row, "ppct")),
                mod.to_float(rv(row, "pconf")),
                mod.to_int(rv(row, "drank")),
                mod.to_float(rv(row, "dnpct")),
                mod.to_int(rv(row, "dnh")),
                mod.text(rv(row, "dnc")),
                mod.text(rv(row, "dns")),
            )

            if len(values) != 28:
                raise RuntimeError(
                    f"internal horse row width mismatch: expected 28, got {len(values)}"
                )

            batch.append(values)
            n += 1

            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                batch.clear()
                if n % 50000 < 5000:
                    print(f"  {n:,}頭", flush=True)

        if batch:
            conn.executemany(sql, batch)

    return n


mod.import_horses = fixed_import_horses


if __name__ == "__main__":
    mod.main()
