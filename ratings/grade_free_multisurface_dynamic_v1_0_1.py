#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launch grade_free_multisurface_dynamic_v1_0 with v1.0.1 surface normalization.

This keeps the v1.0 implementation unchanged and only replaces the surface-family
classifier before main() runs.  It aligns the experiment with the project's
established surface normalization while retaining the new hard split:
TURF / DIRT / SYNTHETIC / OTHER / UNKNOWN.
"""
from __future__ import annotations

import re
import grade_free_multisurface_dynamic_v1_0 as core


def surface_family_v101(value) -> str:
    raw = core._norm(value)
    compact = re.sub(r"[^A-Z0-9一-龠ぁ-んァ-ヶ]+", " ", raw).strip()
    compact_no_space = compact.replace(" ", "")

    if not compact or compact in {"UNKNOWN", "UNK", "N A", "NA", "?", "NONE"}:
        return "UNKNOWN"

    # Synthetic / all-weather is intentionally independent from Dirt.
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


core.surface_family = surface_family_v101
core.SCRIPT_VERSION = "1.0.1-multisurface-hard-split-surfacefix"

if __name__ == "__main__":
    core.main()
