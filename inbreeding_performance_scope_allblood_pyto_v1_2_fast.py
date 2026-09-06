#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbreeding_performance_scope_allblood_pyto_v1_2_fast.py

v1.1高速層の修正版ランチャー。
同じフォルダに以下2ファイルを置く:
  - inbreeding_performance_scope_allblood_pyto_v1_0.py
  - inbreeding_performance_scope_allblood_pyto_v1_1_fast.py

v1.1の高速化をすべて利用しつつ、grouped logisticの基準群を
v1.0本体と同じ「生産国 × 出生5年帯」に厳密に合わせる。
"""

from __future__ import annotations
import csv
import math
import sys

try:
    import inbreeding_performance_scope_allblood_pyto_v1_1_fast as fast
except Exception as e:
    raise SystemExit(
        "同じフォルダに v1_0.py と v1_1_fast.py を置いてください。\n"
        f"import error: {e}"
    )

base = fast.base
VERSION = "1.2.0-fast-fixed-country-era-grouped-logit"
base.VERSION = VERSION

RANK_COL = {
    "Fall": "FallCountryEraPct",
    "F5": "F5CountryEraPct",
    "DeepDelta": "DeepDeltaCountryEraPct",
    "RecentRatio": "RecentRatioCountryEraPct",
}


def _grouped_model_data(con, metric, outcome, where):
    pct = RANK_COL[metric]
    stats = {}
    q = (
        f"SELECT birth_country,birth5,COUNT({metric}),AVG({metric}),AVG({metric}*{metric}) "
        f"FROM ranked_all WHERE {where} AND {metric} IS NOT NULL "
        f"AND birth5 IS NOT NULL GROUP BY birth_country,birth5"
    )
    for country,b5,n,m,m2 in con.execute(q):
        if not n or m is None:
            continue
        var = max(0.0, float(m2) - float(m)*float(m))
        stats[(country,b5)] = (float(m), math.sqrt(var))

    baseline = {}
    q = (
        f"SELECT birth_country,birth5,COUNT(*),SUM({outcome}) "
        f"FROM ranked_all WHERE {where} AND {outcome} IS NOT NULL "
        f"AND birth5 IS NOT NULL GROUP BY birth_country,birth5"
    )
    for country,b5,n,y in con.execute(q):
        if n and y is not None and 0 < y < n:
            p = (float(y)+0.5)/(float(n)+1.0)
            baseline[(country,b5)] = math.log(p/(1-p))

    q = (
        f"SELECT birth_country,birth5,"
        f"CAST(MIN(49,CAST({pct}*50 AS INTEGER)) AS INTEGER) cell,"
        f"COUNT(*),AVG({metric}),SUM({outcome}) "
        f"FROM ranked_all WHERE {where} AND {metric} IS NOT NULL "
        f"AND {pct} IS NOT NULL AND {outcome} IS NOT NULL AND birth5 IS NOT NULL "
        f"GROUP BY birth_country,birth5,cell"
    )
    groups=[]
    total=0
    succ=0
    for country,b5,cell,n,mv,y in con.execute(q):
        st=stats.get((country,b5))
        off=baseline.get((country,b5))
        if not st or off is None or st[1] <= 0 or mv is None:
            continue
        z=(float(mv)-st[0])/st[1]
        groups.append((z,int(n),int(y or 0),off))
        total += int(n)
        succ += int(y or 0)
    return groups,total,succ


def fixed_write_logit(con, out):
    metrics=("Fall","F5","DeepDelta","RecentRatio")
    outcomes=(
        "ScopeFlatStakesTop3","ScopeFlatStakesWin","RepeatWin2","RepeatWin5",
        "Top3_5plus","Mature5plus","MultiYearTop3","G1Reach","G1Win","G2Reach","G3Reach"
    )
    fields=[
        "Population","Metric","Outcome","N","SuccessN","BetaZ","SEZ","PZ",
        "BetaZ2","SEZ2","PZ2","VertexSD","GroupedCells","Note"
    ]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader()
        for pop,where in (
            ("ALL_BLOOD","1=1"),
            ("STAKES_TOP3","ScopeFlatStakesTop3=1"),
        ):
            for metric in metrics:
                for outcome in outcomes:
                    groups,n,s = _grouped_model_data(con,metric,outcome,where)
                    if len(groups)<5 or n<=0 or s<=0 or s>=n:
                        continue
                    fit=fast._fit_grouped(groups)
                    if not fit:
                        continue
                    b,se,v=fit
                    p1=math.erfc(abs(b[1]/se[1])/math.sqrt(2)) if se[1] else None
                    p2=math.erfc(abs(b[2]/se[2])/math.sqrt(2)) if se[2] else None
                    w.writerow({
                        "Population":pop,"Metric":metric,"Outcome":outcome,
                        "N":n,"SuccessN":s,
                        "BetaZ":b[1],"SEZ":se[1],"PZ":p1,
                        "BetaZ2":b[2],"SEZ2":se[2],"PZ2":p2,
                        "VertexSD":v,"GroupedCells":len(groups),
                        "Note":"生産国×出生5年帯を基準に50順位cellへ集約。Vertexは最適近交値ではない"
                    })


base.write_logit = fixed_write_logit


def self_test():
    fast.self_test()
    assert RANK_COL["Fall"] == "FallCountryEraPct"
    assert RANK_COL["F5"] == "F5CountryEraPct"
    print("V1.2 SELF TEST OK: grouped logistic cohort = birth_country × birth5")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        print("="*76)
        print("高速版:", VERSION)
        print("v1.1 cache高速化 + 生産国×出生5年帯 grouped logistic 修正")
        print("="*76)
        base.main()
