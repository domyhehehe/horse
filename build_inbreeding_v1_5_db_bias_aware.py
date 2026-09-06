#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_4_batch_exact.py'
OUT = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_5_db_bias_aware.py'

src = SRC.read_text(encoding='utf-8')
src = re.sub(r'VERSION\s*=\s*"[^"]+"', 'VERSION = "1.5.0-batch-exact-db-bias-aware"', src, count=1)

# Metadataに分析上の前提を残す。
src = src.replace(
    '"grade_rule":"pre-grade era -> NA; grade field only; series_grade never used as grade"',
    '"grade_rule":"pre-grade era -> NA; grade field only; series_grade never used as grade",'
    '"db_bias_aware":True,'
    '"stakes_absence_semantics":"not observed in source stakes CSV / not equivalent to raced-and-lost",'
    '"rank_method":"ALL_BLOOD reference; average-tie midrank; exact zero separated for Fall/F5/DeepDelta bins"'
)

# ロジスティック出力の注意書きもDB観測率であることを明記。
src = src.replace(
    '生産国×出生5年帯を基準に50順位cellへ集約。Vertexは最適近交値ではない',
    '生産国×出生5年帯を基準に50順位cellへ集約。DB内観測モデルであり公式競走成功確率ではない。Vertexは最適近交値ではない'
)

marker = '\n\nif __name__ == "__main__":'
if marker not in src:
    raise SystemExit('main marker not found')

layer = r'''

# ===== v1.5 DB-bias-aware analysis layer =====
# Exact F/F5 coreはv1.4をそのまま使用する。
# ここでは stakes source presence / ALL_BLOOD基準群 / tie / zero / coverage audit を修正する。


def _materialize_scope_stakes_from_cache(sc_path, con, codes, js):
    """normalized stakes cacheから、source presenceとscope Top3を分離してwork DBへ作る。"""
    con.execute("DROP TABLE IF EXISTS stakes_tmp")
    con.execute("DROP TABLE IF EXISTS db_coverage_meta")
    con.execute("CREATE TABLE db_coverage_meta(k TEXT PRIMARY KEY,v INTEGER)")
    con.execute("ATTACH DATABASE ? AS stc", (str(sc_path),))

    clause, params = _scope_where(codes, js)
    gs = grade_start_for_scope(codes, js)
    grade_ok = "0" if gs is None else f"CASE WHEN r.race_year>={int(gs)} THEN 1 ELSE 0 END"

    # scope内Top3集約。順位・成績はここだけで作る。
    con.execute("DROP TABLE IF EXISTS _scope_agg")
    con.execute(f"""
      CREATE TEMP TABLE _scope_agg AS
      SELECT
        r.pk,
        COUNT(*) AS top3_n,
        SUM(CASE WHEN r.place=1 THEN 1 ELSE 0 END) AS win_n,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade_present=1 THEN 1 ELSE 0 END) AS grade_obs,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G1' THEN 1 ELSE 0 END) AS g1,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G1' AND r.place=1 THEN 1 ELSE 0 END) AS g1w,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G2' THEN 1 ELSE 0 END) AS g2,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G2' AND r.place=1 THEN 1 ELSE 0 END) AS g2w,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G3' THEN 1 ELSE 0 END) AS g3,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G3' AND r.place=1 THEN 1 ELSE 0 END) AS g3w,
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='LISTED' THEN 1 ELSE 0 END) AS listed,
        CASE WHEN COUNT(DISTINCT r.race_year)>=2 THEN 1 ELSE 0 END AS multi_year,
        MAX(CASE
              WHEN r.age BETWEEN 1 AND 20 THEN r.age
              WHEN r.race_year IS NOT NULL AND h.birth_year IS NOT NULL
                   AND r.race_year-h.birth_year BETWEEN 1 AND 20 THEN r.race_year-h.birth_year
            END) AS final_age,
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=2 THEN 1 ELSE 0 END) AS a2,
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=3 THEN 1 ELSE 0 END) AS a3,
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=4 THEN 1 ELSE 0 END) AS a4,
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=5 THEN 1 ELSE 0 END) AS a5,
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)>=6 THEN 1 ELSE 0 END) AS a6,
        MAX(CASE WHEN r.surface='TURF' THEN 1 ELSE 0 END) AS turf,
        MAX(CASE WHEN r.surface='TURF' AND r.place=1 THEN 1 ELSE 0 END) AS turfw,
        MAX(CASE WHEN r.surface='DIRT' THEN 1 ELSE 0 END) AS dirt,
        MAX(CASE WHEN r.surface='DIRT' AND r.place=1 THEN 1 ELSE 0 END) AS dirtw,
        MAX(CASE WHEN r.dist_band='D1000_1300' THEN 1 ELSE 0 END) AS d1,
        MAX(CASE WHEN r.dist_band='D1000_1300' AND r.place=1 THEN 1 ELSE 0 END) AS d1w,
        MAX(CASE WHEN r.dist_band='D1400_1600' THEN 1 ELSE 0 END) AS d2,
        MAX(CASE WHEN r.dist_band='D1400_1600' AND r.place=1 THEN 1 ELSE 0 END) AS d2w,
        MAX(CASE WHEN r.dist_band='D1700_2000' THEN 1 ELSE 0 END) AS d3,
        MAX(CASE WHEN r.dist_band='D1700_2000' AND r.place=1 THEN 1 ELSE 0 END) AS d3w,
        MAX(CASE WHEN r.dist_band='D2100_2400' THEN 1 ELSE 0 END) AS d4,
        MAX(CASE WHEN r.dist_band='D2100_2400' AND r.place=1 THEN 1 ELSE 0 END) AS d4w,
        MAX(CASE WHEN r.dist_band='D2500P' THEN 1 ELSE 0 END) AS d5,
        MAX(CASE WHEN r.dist_band='D2500P' AND r.place=1 THEN 1 ELSE 0 END) AS d5w
      FROM stc.race r
      LEFT JOIN horse h ON h.pk=r.pk
      WHERE {clause}
      GROUP BY r.pk
    """, params)
    con.execute("CREATE INDEX IF NOT EXISTS idx_scope_agg_pk ON _scope_agg(pk)")

    # source_horseはscopeやTop3とは無関係。元stakes CSVにPK行があるかだけを表す。
    # これにより StakesRowPresent と ScopeFlatStakesTop3 が独立する。
    con.execute("""CREATE TABLE stakes_tmp AS
      SELECT
        sh.pk AS pk,
        1 AS row_present,
        COALESCE(a.top3_n,0) AS top3_n,
        COALESCE(a.win_n,0) AS win_n,
        COALESCE(a.grade_obs,0) AS grade_obs,
        COALESCE(a.g1,0) AS g1, COALESCE(a.g1w,0) AS g1w,
        COALESCE(a.g2,0) AS g2, COALESCE(a.g2w,0) AS g2w,
        COALESCE(a.g3,0) AS g3, COALESCE(a.g3w,0) AS g3w,
        COALESCE(a.listed,0) AS listed,
        COALESCE(a.multi_year,0) AS multi_year,
        a.final_age AS final_age,
        COALESCE(a.a2,0) AS a2, COALESCE(a.a3,0) AS a3,
        COALESCE(a.a4,0) AS a4, COALESCE(a.a5,0) AS a5, COALESCE(a.a6,0) AS a6,
        COALESCE(a.turf,0) AS turf, COALESCE(a.turfw,0) AS turfw,
        COALESCE(a.dirt,0) AS dirt, COALESCE(a.dirtw,0) AS dirtw,
        COALESCE(a.d1,0) AS d1, COALESCE(a.d1w,0) AS d1w,
        COALESCE(a.d2,0) AS d2, COALESCE(a.d2w,0) AS d2w,
        COALESCE(a.d3,0) AS d3, COALESCE(a.d3w,0) AS d3w,
        COALESCE(a.d4,0) AS d4, COALESCE(a.d4w,0) AS d4w,
        COALESCE(a.d5,0) AS d5, COALESCE(a.d5w,0) AS d5w
      FROM stc.source_horse sh
      LEFT JOIN _scope_agg a ON a.pk=sh.pk
    """)
    con.execute("CREATE UNIQUE INDEX idx_stakes_tmp_pk ON stakes_tmp(pk)")

    counts = {
        'source_unique_pk': con.execute("SELECT COUNT(*) FROM stc.source_horse").fetchone()[0],
        'source_matched_blood_pk': con.execute("SELECT COUNT(*) FROM stc.source_horse s JOIN horse h ON h.pk=s.pk").fetchone()[0],
        'scope_top3_unique_pk': con.execute("SELECT COUNT(*) FROM _scope_agg").fetchone()[0],
        'scope_top3_matched_blood_pk': con.execute("SELECT COUNT(*) FROM _scope_agg a JOIN horse h ON h.pk=a.pk").fetchone()[0],
    }
    counts['source_unmatched_pk'] = counts['source_unique_pk'] - counts['source_matched_blood_pk']
    counts['scope_top3_unmatched_pk'] = counts['scope_top3_unique_pk'] - counts['scope_top3_matched_blood_pk']
    con.executemany("INSERT OR REPLACE INTO db_coverage_meta(k,v) VALUES(?,?)", list(counts.items()))
    con.commit()
    con.execute("DETACH DATABASE stc")
    return counts


def dbbias_parse_stakes_to_db(paths, con, codes, js):
    sc, hit = _stakes_cache_open(paths)
    sc.commit()
    sc_path = Path(sc.execute("PRAGMA database_list").fetchone()[2])
    sc.close()
    counts = _materialize_scope_stakes_from_cache(sc_path, con, codes, js)
    n = con.execute("SELECT COUNT(*) FROM stakes_tmp WHERE top3_n>0").fetchone()[0]
    rec = con.execute("SELECT COALESCE(SUM(top3_n),0) FROM stakes_tmp").fetchone()[0]
    print(f"stakes source PK: {counts['source_unique_pk']:,} / blood一致 {counts['source_matched_blood_pk']:,}")
    print(f"scope平地Top3: {n:,}頭 / {int(rec):,}件  (normalized cache {'HIT' if hit else 'BUILT'})")


# main() が呼ぶ名前を差し替える。
fast_parse_stakes_to_db = dbbias_parse_stakes_to_db
parse_stakes_to_db = dbbias_parse_stakes_to_db


def _midrank_sql(metric, positive_only=False):
    """country×birth5内の平均tie percentile用SQL部品。NULLは順位母集団から除外。"""
    if positive_only:
        val = f"CASE WHEN {metric}>0 THEN {metric} END"
        valid = f"{metric}>0"
    else:
        val = metric
        valid = f"{metric} IS NOT NULL"
    order = f"CASE WHEN {valid} THEN 0 ELSE 1 END, {val}"
    rank = f"RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY {order})"
    tie = f"COUNT(*) OVER(PARTITION BY birth_country,birth5,{val})"
    n = f"COUNT({val}) OVER(PARTITION BY birth_country,birth5)"
    return rank, tie, n, valid


def dbbias_add_rank_tables(con):
    """順位は必ずALL_BLOODを基準に一度だけ作る。tiesは平均順位。"""
    con.execute("DROP TABLE IF EXISTS ranked_all")
    metrics = ["Fall","F5","DeepDelta","RecentRatio","F5SourceHHI","F5MaxSourceShare"]
    inner = []
    for m in metrics:
        r,t,n,valid = _midrank_sql(m, False)
        inner += [f"{r} AS __{m}_r", f"{t} AS __{m}_t", f"{n} AS __{m}_n"]
    for m in ("Fall","F5","DeepDelta"):
        r,t,n,valid = _midrank_sql(m, True)
        inner += [f"{r} AS __{m}_pr", f"{t} AS __{m}_pt", f"{n} AS __{m}_pn"]

    con.execute("DROP TABLE IF EXISTS _rank_base")
    con.execute("CREATE TEMP TABLE _rank_base AS SELECT h.*," + ",".join(inner) + " FROM horse h WHERE AnalysisBirthYearEligible=1")

    def pct(m):
        return f"CASE WHEN {m} IS NULL THEN NULL WHEN __{m}_n<=1 THEN 0.5 ELSE ((__{m}_r-1)+(__{m}_t-1)/2.0)/(__{m}_n-1) END"
    def ppct(m):
        return f"CASE WHEN {m}<=0 OR {m} IS NULL THEN NULL WHEN __{m}_pn<=1 THEN 0.5 ELSE ((__{m}_pr-1)+(__{m}_pt-1)/2.0)/(__{m}_pn-1) END"

    con.execute(f"""CREATE TABLE ranked_all AS SELECT
      *,
      {pct('Fall')} AS FallCountryEraPct,
      {pct('F5')} AS F5CountryEraPct,
      {pct('DeepDelta')} AS DeepDeltaCountryEraPct,
      {pct('RecentRatio')} AS RecentRatioCountryEraPct,
      {pct('F5SourceHHI')} AS F5SourceHHICountryEraPct,
      {pct('F5MaxSourceShare')} AS F5MaxSourceShareCountryEraPct,
      {ppct('Fall')} AS FallPositiveCountryEraPct,
      {ppct('F5')} AS F5PositiveCountryEraPct,
      {ppct('DeepDelta')} AS DeepDeltaPositiveCountryEraPct,
      CASE WHEN ABS(COALESCE(Fall,0))<1e-12 THEN 1 ELSE 0 END AS FallZeroFlag,
      CASE WHEN ABS(COALESCE(F5,0))<1e-12 THEN 1 ELSE 0 END AS F5ZeroFlag,
      CASE WHEN ABS(COALESCE(DeepDelta,0))<1e-12 THEN 1 ELSE 0 END AS DeepDeltaZeroFlag
      FROM _rank_base""")
    # 内部window補助列は外部CSVへ出さないため別tableで削る。
    keep = [r[1] for r in con.execute("PRAGMA table_info(ranked_all)") if not r[1].startswith('__')]
    con.execute("ALTER TABLE ranked_all RENAME TO _ranked_all_raw")
    con.execute("CREATE TABLE ranked_all AS SELECT " + ",".join('"'+x.replace('"','""')+'"' for x in keep) + " FROM _ranked_all_raw")
    con.execute("DROP TABLE _ranked_all_raw")
    con.execute("DROP TABLE _rank_base")
    con.execute("CREATE INDEX idx_ranked_all_ce ON ranked_all(birth_country,birth5)")
    con.execute("CREATE INDEX idx_ranked_all_sire5 ON ranked_all(sire,birth5)")
    con.commit()


add_rank_tables = dbbias_add_rank_tables


DBBIAS_METRICS = (
    ("Fall","FallPositiveCountryEraPct",True),
    ("F5","F5PositiveCountryEraPct",True),
    ("DeepDelta","DeepDeltaPositiveCountryEraPct",True),
    ("RecentRatio","RecentRatioCountryEraPct",False),
    ("F5SourceHHI","F5SourceHHICountryEraPct",False),
    ("F5MaxSourceShare","F5MaxSourceShareCountryEraPct",False),
)


def _write_bin_row(w, pop_name, metric, band, g, rank_note):
    if not g:
        return
    rec={"Population":pop_name,"Metric":metric,"Band":band,"N":len(g),
         "MetricMean":sum(float(x[0]) for x in g)/len(g),
         "Top3MeanN":sum(x[2] or 0 for x in g)/len(g),"WinMeanN":sum(x[3] or 0 for x in g)/len(g),
         "RankReference":rank_note}
    top=sum(x[2] or 0 for x in g); wins=sum(x[3] or 0 for x in g)
    rec["WinConversion"]=wins/top if top else ""
    base=4
    for j,o in enumerate(OUTCOMES):
        vv=[x[base+j] for x in g if x[base+j] is not None]
        rec[o+"Rate"]=sum(vv)/len(vv) if vv else ""
    rec["GradeEligibleN"]=sum(1 for x in g if x[-1]==1)
    w.writerow(rec)


def dbbias_summarize_bins(con,out,pop_name,where_extra="1=1"):
    fields=["Population","Metric","Band","N","MetricMean","Top3MeanN","WinMeanN","WinConversion"]+[x+"Rate" for x in OUTCOMES]+["GradeEligibleN","RankReference"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for metric,pct,zero_sep in DBBIAS_METRICS:
            cols=",".join([metric,pct,"ScopeTop3N","ScopeWinN",*OUTCOMES,"GradeEraEligible"])
            if zero_sep:
                z=list(con.execute(f"SELECT {cols} FROM ranked_all WHERE {where_extra} AND ABS(COALESCE({metric},0))<1e-12"))
                _write_bin_row(w,pop_name,metric,"ZERO",z,"ALL_BLOOD; exact zero separate")
                q=f"SELECT {cols} FROM ranked_all WHERE {where_extra} AND {metric}>0 AND {pct} IS NOT NULL"
                note="ALL_BLOOD positive-only average-tie midrank"
            else:
                q=f"SELECT {cols} FROM ranked_all WHERE {where_extra} AND {metric} IS NOT NULL AND {pct} IS NOT NULL"
                note="ALL_BLOOD average-tie midrank"
            groups=defaultdict(list)
            for row in con.execute(q): groups[band_of(row[1])].append(row)
            for _,_,b in F_BANDS:
                _write_bin_row(w,pop_name,metric,b,groups.get(b,[]),note)


summarize_bins = dbbias_summarize_bins


def _inner_midrank_table(con, name, outer_metric, outer_pos_pct, inner_metric):
    con.execute("DROP TABLE IF EXISTS _cbase")
    con.execute("DROP TABLE IF EXISTS _crank")
    # outer=0は独立stratum。positiveはcountry×birth5 positive percentileで20分割。
    con.execute(f"""CREATE TEMP TABLE _cbase AS SELECT *,
      CASE WHEN ABS(COALESCE({outer_metric},0))<1e-12 THEN -1
           WHEN {outer_pos_pct} IS NOT NULL THEN MIN(19,CAST({outer_pos_pct}*20 AS INTEGER)) END AS outer20
      FROM ranked_all
      WHERE {inner_metric} IS NOT NULL
        AND (ABS(COALESCE({outer_metric},0))<1e-12 OR {outer_pos_pct} IS NOT NULL)
    """)
    # groupはALL_BLOOD全体で一度だけ決める。innerが全tieのouter cellは比較不能なので除外。
    con.execute(f"""CREATE TEMP TABLE _crank AS
      WITH z AS (
        SELECT *,
          RANK() OVER(PARTITION BY birth_country,birth5,outer20 ORDER BY {inner_metric}) AS ir,
          COUNT(*) OVER(PARTITION BY birth_country,birth5,outer20,{inner_metric}) AS it,
          COUNT(*) OVER(PARTITION BY birth_country,birth5,outer20) AS inn,
          MIN({inner_metric}) OVER(PARTITION BY birth_country,birth5,outer20) AS imin,
          MAX({inner_metric}) OVER(PARTITION BY birth_country,birth5,outer20) AS imax
        FROM _cbase WHERE outer20 IS NOT NULL
      )
      SELECT *, CASE WHEN imax>imin AND inn>1 THEN ((ir-1)+(it-1)/2.0)/(inn-1) END AS innerpct
      FROM z
    """)


def dbbias_summarize_conditional(con,out):
    fields=["Population","Direction","InnerGroup","N","FallMean","F5Mean","DeepDeltaMean"]+[x+"Rate" for x in OUTCOMES]+["GroupReference"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        specs=(
            ("Fall固定→F5","Fall","FallPositiveCountryEraPct","F5"),
            ("F5固定→Fall","F5","F5PositiveCountryEraPct","Fall"),
        )
        for name,om,op,im in specs:
            _inner_midrank_table(con,name,om,op,im)
            for pop,where in (("ALL_BLOOD","1=1"),("STAKES_TOP3","ScopeFlatStakesTop3=1")):
                q="SELECT CASE WHEN innerpct<0.333333 THEN 'LOW' WHEN innerpct<0.666667 THEN 'MID' ELSE 'HIGH' END g,COUNT(*),AVG(Fall),AVG(F5),AVG(DeepDelta),"+",".join(f"AVG({o})" for o in OUTCOMES)+f" FROM _crank WHERE innerpct IS NOT NULL AND {where} GROUP BY g"
                for row in con.execute(q):
                    rec={"Population":pop,"Direction":name,"InnerGroup":row[0],"N":row[1],"FallMean":row[2],"F5Mean":row[3],"DeepDeltaMean":row[4],"GroupReference":"ALL_BLOOD country×birth5 outer-stratum; average-tie midrank; no-N-cutoff"}
                    for i,o in enumerate(OUTCOMES): rec[o+"Rate"]=row[5+i]
                    w.writerow(rec)


summarize_conditional = dbbias_summarize_conditional


def _sire_rank_table(con, metric):
    con.execute("DROP TABLE IF EXISTS _sire_rank")
    # ZEROは独立クラス。positiveだけをALL_BLOODの同父×出生5年帯で平均tie順位化。
    con.execute(f"""CREATE TEMP TABLE _sire_rank AS
      WITH z AS (
        SELECT *,
          COUNT(*) OVER(PARTITION BY sire,birth5) AS celln,
          RANK() OVER(PARTITION BY sire,birth5 ORDER BY CASE WHEN {metric}>0 THEN 0 ELSE 1 END, CASE WHEN {metric}>0 THEN {metric} END) AS pr,
          COUNT(*) OVER(PARTITION BY sire,birth5,CASE WHEN {metric}>0 THEN {metric} END) AS pt,
          COUNT(CASE WHEN {metric}>0 THEN 1 END) OVER(PARTITION BY sire,birth5) AS pn
        FROM ranked_all
        WHERE sire<>'' AND birth5 IS NOT NULL AND {metric} IS NOT NULL
      )
      SELECT *,
        CASE WHEN ABS(COALESCE({metric},0))<1e-12 THEN 'ZERO'
             WHEN pn<=1 THEN 'MID50'
             WHEN ((pr-1)+(pt-1)/2.0)/(pn-1)<0.25 THEN 'LOW25'
             WHEN ((pr-1)+(pt-1)/2.0)/(pn-1)<0.75 THEN 'MID50'
             ELSE 'HIGH25' END AS sire_group
      FROM z
    """)


def dbbias_summarize_sire_birth5(con,out):
    fields=["Population","Metric","Group","N","MeanMetric","MeanCellN"]+[x+"Rate" for x in OUTCOMES]+["GroupReference"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for metric in ("Fall","F5","DeepDelta"):
            _sire_rank_table(con,metric)
            for pop,where in (("ALL_BLOOD","1=1"),("STAKES_TOP3","ScopeFlatStakesTop3=1")):
                q="SELECT sire_group,COUNT(*),AVG("+metric+"),AVG(celln),"+",".join(f"AVG({o})" for o in OUTCOMES)+f" FROM _sire_rank WHERE {where} GROUP BY sire_group ORDER BY CASE sire_group WHEN 'ZERO' THEN 0 WHEN 'LOW25' THEN 1 WHEN 'MID50' THEN 2 ELSE 3 END"
                for row in con.execute(q):
                    rec={"Population":pop,"Metric":metric,"Group":row[0],"N":row[1],"MeanMetric":row[2],"MeanCellN":row[3],"GroupReference":"ALL_BLOOD same-sire×birth5; ZERO separate; positive average-tie midrank; no-N-cutoff"}
                    for i,o in enumerate(OUTCOMES): rec[o+"Rate"]=row[4+i]
                    w.writerow(rec)


summarize_sire_birth5 = dbbias_summarize_sire_birth5


def write_db_coverage_audit(con,out):
    fields=["Level","BirthCountry","Birth5","BloodN","StakesSourceRowN","StakesSourceRowRate","ScopeTop3N","ScopeTop3ObservedRate","ScopeTop3AmongSourceRate"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        queries=(
            ("ALL","SELECT '' c,NULL b,COUNT(*),SUM(StakesRowPresent),SUM(ScopeFlatStakesTop3) FROM horse WHERE AnalysisBirthYearEligible=1"),
            ("COUNTRY","SELECT birth_country,NULL b,COUNT(*),SUM(StakesRowPresent),SUM(ScopeFlatStakesTop3) FROM horse WHERE AnalysisBirthYearEligible=1 GROUP BY birth_country"),
            ("COUNTRY_BIRTH5","SELECT birth_country,birth5,COUNT(*),SUM(StakesRowPresent),SUM(ScopeFlatStakesTop3) FROM horse WHERE AnalysisBirthYearEligible=1 GROUP BY birth_country,birth5"),
        )
        for level,q in queries:
            for c,b,n,s,t in con.execute(q):
                n=int(n or 0); s=int(s or 0); t=int(t or 0)
                w.writerow({"Level":level,"BirthCountry":c or "","Birth5":"" if b is None else b,"BloodN":n,"StakesSourceRowN":s,
                            "StakesSourceRowRate":s/n if n else "","ScopeTop3N":t,"ScopeTop3ObservedRate":t/n if n else "",
                            "ScopeTop3AmongSourceRate":t/s if s else ""})


def dbbias_write_report(con,path,blood,stakes,scope,gs,ystart,yend):
    total=con.execute("SELECT COUNT(*) FROM horse").fetchone()[0]
    analy=con.execute("SELECT COUNT(*) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0]
    source=con.execute("SELECT COALESCE(SUM(StakesRowPresent),0) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0]
    top3=con.execute("SELECT COALESCE(SUM(ScopeFlatStakesTop3),0) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0]
    grade=con.execute("SELECT COALESCE(SUM(CASE WHEN GradeEraEligible=1 THEN 1 ELSE 0 END),0) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0]
    meta={k:int(v) for k,v in con.execute("SELECT k,v FROM db_coverage_meta")} if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='db_coverage_meta'").fetchone()[0] else {}
    source_rate=(source/analy) if analy else 0
    top3_rate=(top3/analy) if analy else 0
    among=(top3/source) if source else 0
    txt=f"""近交係数 × 平地stakes 多軸監査
Version: {VERSION}

入力
  blood: {blood}
  stakes:
"""+"\n".join("    "+str(x) for x in stakes)+f"""

開催scope: {scope}
対象馬出生年: {ystart if ystart is not None else 'ALL'} ～ {yend if yend is not None else 'ALL'}

blood全頭N: {total:,}
解析出生年内N: {analy:,}
stakes元CSVにPK行があるblood馬N: {int(source):,} ({source_rate:.3%})
scope内平地stakes Top3観測N: {int(top3):,} ({top3_rate:.3%} of blood / {among:.3%} of source-present)
Grade時代評価可能N: {int(grade):,}
Grade開始年: {gs if gs is not None else 'scopeが複数国/未定義のため一律分母を作らない'}

stakes source PK監査
  source unique PK: {meta.get('source_unique_pk',0):,}
  blood一致: {meta.get('source_matched_blood_pk',0):,}
  blood未解決: {meta.get('source_unmatched_pk',0):,}
  scope Top3 unique PK: {meta.get('scope_top3_unique_pk',0):,}
  scope Top3 blood一致: {meta.get('scope_top3_matched_blood_pk',0):,}
  scope Top3 blood未解決: {meta.get('scope_top3_unmatched_pk',0):,}

DB偏りについて（最重要）
・stakes_horsesは公式の全出走馬DBではない。PedigreeQuery系の成功馬・注目馬寄り収録を含むため、ScopeFlatStakesTop3=0を「出走して負けた」と解釈しない。
・StakesRowPresent=1 は「元stakes CSVにそのPK行が存在する」のみを意味する。scope内Top3とは独立。
・ScopeFlatStakesTop3 は「このDBでscope内平地stakes Top3として観測された」指標であり、公式競走成功確率ではない。
・inbreeding_db_coverage_audit.csv で国×出生5年帯ごとのsource収録率とTop3観測率を必ず併読する。
・年代・地域・父を揃えた内部比較を優先し、時代を跨ぐ絶対率の差を生物学的効果と即断しない。

順位・群分け
・順位の参照母集団は常にALL_BLOOD。STAKES_TOP3だけで順位を作り直さない。
・tieはPERCENT_RANKの最小順位ではなく平均tie順位(midrank)を使う。
・Fall/F5/DeepDeltaのexact 0はbinと同父比較でZEROとして独立表示。正値のpercentileは正値馬だけで作る。
・conditional depthはALL_BLOODでouter/inner群を一度決め、その同じ群定義をSTAKES_TOP3 subsetにも適用する。
・人数による除外は一切しない。比較不能な「inner値が全頭同値」のcellのみconditional比較から外す。

その他
・all_horse_inbreeding_exact*.csvは不要。Fall/F5はblood.csvから内部計算。
・Grade制度以前はG1/G2/G3を0にしない。GradeEraEligible=0なら各Grade列はNA。
・JRAは1984、NARは1997、JPN_ALLは保守的に1997をGrade比較開始年とする。
・USA/CAN=1973、GB/IRE/FR=1971。未知scopeではGrade分母を自動生成しない。
・Fall/F5/DeepDelta/RecentRatioは能力点ではない。
・低F側低下をoutbreeding depressionと即断しない。
・高F側の不利と「Fを下げれば強くなる」は別命題。
・二次曲線Vertexを育種上の最適Fにしない。
"""
    Path(path).write_text(txt,encoding="utf-8")
    write_db_coverage_audit(con, Path(path).parent/"inbreeding_db_coverage_audit.csv")


write_report = dbbias_write_report


def self_test_db_bias_layer():
    import tempfile
    # 1) midrank: 0,0,1,2 -> zero tie midrank=1/6; positive 1,2 -> 0,1
    c=sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE horse(pk TEXT,birth_country TEXT,birth5 INTEGER,Fall REAL,F5 REAL,DeepDelta REAL,RecentRatio REAL,F5SourceHHI REAL,F5MaxSourceShare REAL,AnalysisBirthYearEligible INTEGER,sire TEXT,ScopeFlatStakesTop3 INTEGER)""")
    vals=[('a','X',2000,0,0,0,None,None,None,1,'S',0),('b','X',2000,0,0,0,None,None,None,1,'S',1),('c','X',2000,1,1,0,1,.5,.5,1,'S',0),('d','X',2000,2,1,1,.5,.5,.5,1,'S',1)]
    c.executemany("INSERT INTO horse VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",vals)
    dbbias_add_rank_tables(c)
    z=c.execute("SELECT FallCountryEraPct FROM ranked_all WHERE pk='a'").fetchone()[0]
    p1=c.execute("SELECT FallPositiveCountryEraPct FROM ranked_all WHERE pk='c'").fetchone()[0]
    p2=c.execute("SELECT FallPositiveCountryEraPct FROM ranked_all WHERE pk='d'").fetchone()[0]
    assert abs(z-(1/6))<1e-10,(z,1/6)
    assert abs(p1-0.0)<1e-10 and abs(p2-1.0)<1e-10,(p1,p2)
    assert c.execute("SELECT FallZeroFlag FROM ranked_all WHERE pk='a'").fetchone()[0]==1
    c.close()

    # 2) source presenceはscope top3が0でも1を保持。
    with tempfile.TemporaryDirectory() as td:
        sp=Path(td)/'st.sqlite3'
        sc=sqlite3.connect(str(sp))
        sc.execute("CREATE TABLE source_horse(pk TEXT PRIMARY KEY)")
        sc.execute("CREATE TABLE race(pk TEXT,country TEXT,venue TEXT,race_year INTEGER,place INTEGER,grade TEXT,grade_present INTEGER,surface TEXT,dist_band TEXT,age INTEGER)")
        sc.executemany("INSERT INTO source_horse VALUES(?)", [('A',),('B',),('C',),('UNRES',)])
        sc.executemany("INSERT INTO race VALUES(?,?,?,?,?,?,?,?,?,?)", [
            ('A','JPN','JRA',2020,1,'G1',1,'TURF','D1700_2000',3),
            ('C','USA','',2020,2,'G1',1,'DIRT','D1700_2000',3),
            ('UNRES','JPN','JRA',2020,3,'',0,'TURF','D1400_1600',3),
        ])
        sc.commit(); sc.close()
        w=sqlite3.connect(":memory:")
        w.execute("CREATE TABLE horse(pk TEXT PRIMARY KEY,birth_year INTEGER)")
        w.executemany("INSERT INTO horse VALUES(?,?)", [('A',2017),('B',2017),('C',2017)])
        counts=_materialize_scope_stakes_from_cache(sp,w,['JPN'],'JRA')
        assert w.execute("SELECT row_present,top3_n FROM stakes_tmp WHERE pk='B'").fetchone()==(1,0)
        assert w.execute("SELECT row_present,top3_n FROM stakes_tmp WHERE pk='A'").fetchone()==(1,1)
        assert counts['source_unique_pk']==4 and counts['source_matched_blood_pk']==3
        assert counts['scope_top3_unique_pk']==2 and counts['scope_top3_matched_blood_pk']==1
        w.close()
    print("V1.5 DB BIAS SELF TEST OK: source presence / midrank / zero handling")


# self-test entryを拡張。
_old_self_test_all_in_one_v14 = self_test_all_in_one_v14
def self_test_all_in_one_v15():
    _old_self_test_all_in_one_v14()
    self_test_db_bias_layer()
'''

src = src.replace(marker, layer + marker, 1)
src = src.replace('self_test_all_in_one_v14()\n    else:', 'self_test_all_in_one_v15()\n    else:', 1)
src = src.replace('BATCH EXACT ALL-IN-ONE", VERSION', 'BATCH EXACT DB-BIAS-AWARE ALL-IN-ONE", VERSION')
src = src.replace('生産国×出生5年帯 grouped logistic / JPN_ALL・JRA・NAR / blood全頭出力', 'ALL_BLOOD基準midrank / true stakes source presence / DB coverage audit / JPN_ALL・JRA・NAR')

OUT.write_text(src, encoding='utf-8')
print(OUT)
print('bytes=', OUT.stat().st_size)
