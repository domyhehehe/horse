#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbreeding_performance_scope_allblood_pyto_v1_1_fast.py

高速化アドオン版。v1.0 と同じフォルダに置いて実行する。

v1.0 の解析仕様は維持しつつ、重い部分だけ差し替える。

主な変更
--------
1. 全世代Exact Fのkinship pair cacheを「上限到達で全消去」からLRUへ変更。
2. F5もLRU cache化。
3. 5代祖先pathは、複数産駒で再利用される親だけcache。
4. stakes_horses*.csv のJSONを初回だけ正規化しSQLiteへ保存。
   2回目以降、JRA/NAR/USA等のscope変更では巨大JSONを再parseしない。
5. v1.0の既存Exact F/F5 SQLiteをそのまま再利用する。
6. 二次logisticは個体を毎iteration直接なめず、出生5年帯×F順位cellへ集約してから解く。

必須:
  同じフォルダに inbreeding_performance_scope_allblood_pyto_v1_0.py

出力形式・blood全頭出力・JPN_ALL/JRA/NAR・Grade以前NA・人数cutoffなしはv1.0を継承。
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

try:
    import inbreeding_performance_scope_allblood_pyto_v1_0 as base
except Exception as e:
    raise SystemExit(
        "同じフォルダに inbreeding_performance_scope_allblood_pyto_v1_0.py を置いてください。\n"
        f"import error: {e}"
    )

VERSION = "1.1.0-fast-lru-stakes-cache-grouped-logit"
base.VERSION = VERSION
STAKES_CACHE_DB = base.SCRIPT_DIR / ".inbreeding_stakes_normalized_v2.sqlite3"


class FastExactFEngine:
    """v1.0と同じ再帰式。pair cacheだけLRU化して全消去を廃止。"""
    def __init__(self, by, pre_f=None, max_pairs=1_500_000):
        self.by = by
        self.depth_cache = {}
        self.f_cache = dict(pre_f or {})
        self.depth_stack = set()
        self.f_stack = set()
        self.kin_stack = set()
        self._kin_cached = lru_cache(maxsize=max_pairs)(self._kin_impl)

    def parents(self, pk):
        h = self.by.get(pk)
        if not h:
            return "", ""
        s = h.sire if h.sire and h.sire != pk and h.sire in self.by else ""
        d = h.dam if h.dam and h.dam != pk and h.dam in self.by else ""
        return s, d

    def depth(self, pk):
        if not pk or pk not in self.by:
            return 0
        if pk in self.depth_cache:
            return self.depth_cache[pk]
        if pk in self.depth_stack:
            return 0
        self.depth_stack.add(pk)
        s, d = self.parents(pk)
        vals = [self.depth(x) for x in (s, d) if x]
        z = 1 + max(vals) if vals else 0
        self.depth_stack.discard(pk)
        self.depth_cache[pk] = z
        return z

    def f(self, pk):
        if pk in self.f_cache:
            return self.f_cache[pk]
        if pk in self.f_stack:
            return 0.0
        self.f_stack.add(pk)
        s, d = self.parents(pk)
        z = self.kin(s, d) if s and d else 0.0
        self.f_stack.discard(pk)
        self.f_cache[pk] = z
        return z

    def _which_expand(self, a, b):
        sa, da = self.parents(a)
        sb, db = self.parents(b)
        ha = bool(sa or da)
        hb = bool(sb or db)
        if ha and not hb:
            return 0
        if hb and not ha:
            return 1
        if not ha and not hb:
            return -1
        depa, depb = self.depth(a), self.depth(b)
        if depa != depb:
            return 0 if depa > depb else 1
        ya = self.by[a].year
        yb = self.by[b].year
        if ya is not None and yb is not None and ya != yb:
            return 0 if ya > yb else 1
        return 0 if a > b else 1

    def kin(self, a, b):
        if not a or not b or a not in self.by or b not in self.by:
            return 0.0
        if b < a:
            a, b = b, a
        return self._kin_cached(a, b)

    def _kin_impl(self, a, b):
        key = (a, b)
        if key in self.kin_stack:
            return 0.0
        self.kin_stack.add(key)
        try:
            if a == b:
                return 0.5 * (1.0 + self.f(a))
            side = self._which_expand(a, b)
            if side < 0:
                return 0.0
            if side == 0:
                s, d = self.parents(a)
                return 0.5 * ((self.kin(s, b) if s else 0.0) + (self.kin(d, b) if d else 0.0))
            s, d = self.parents(b)
            return 0.5 * ((self.kin(a, s) if s else 0.0) + (self.kin(a, d) if d else 0.0))
        finally:
            self.kin_stack.discard(key)

    def cache_info(self):
        return self._kin_cached.cache_info()


class FastF5Engine:
    """5代打切りkinship。v1.0式をLRU化。"""
    def __init__(self, by, depth_engine, max_pairs=700_000):
        self.by = by
        self.depth_engine = depth_engine
        self.fm = {}
        self.stack = set()
        self._phi_cached = lru_cache(maxsize=max_pairs)(self._phi_impl)

    def parents(self, pk):
        return self.depth_engine.parents(pk)

    def fnode(self, pk, k):
        if not pk or pk not in self.by or k <= 0:
            return 0.0
        key = (pk, k)
        if key in self.fm:
            return self.fm[key]
        s, d = self.parents(pk)
        z = self.phi(s, d, k - 1, k - 1) if s and d else 0.0
        self.fm[key] = z
        return z

    def phi(self, a, b, ka, kb):
        if not a or not b or a not in self.by or b not in self.by:
            return 0.0
        left = (a, ka)
        right = (b, kb)
        if right < left:
            a, b, ka, kb = b, a, kb, ka
        return self._phi_cached(a, b, ka, kb)

    def _phi_impl(self, a, b, ka, kb):
        key = (a, b, ka, kb)
        if key in self.stack:
            return 0.0
        self.stack.add(key)
        try:
            if a == b:
                return 0.5 * (1.0 + self.fnode(a, max(ka, kb)))
            if ka <= 0 and kb <= 0:
                return 0.0
            sa, da = self.parents(a)
            sb, db = self.parents(b)
            cana = ka > 0 and bool(sa or da)
            canb = kb > 0 and bool(sb or db)
            if cana and not canb:
                side = 0
            elif canb and not cana:
                side = 1
            elif not cana and not canb:
                return 0.0
            else:
                depa = self.depth_engine.depth(a)
                depb = self.depth_engine.depth(b)
                if depa != depb:
                    side = 0 if depa > depb else 1
                else:
                    ya = self.by[a].year
                    yb = self.by[b].year
                    if ya is not None and yb is not None and ya != yb:
                        side = 0 if ya > yb else 1
                    else:
                        side = 0 if a > b else 1
            if side == 0:
                return 0.5 * (
                    (self.phi(sa, b, ka - 1, kb) if sa else 0.0) +
                    (self.phi(da, b, ka - 1, kb) if da else 0.0)
                )
            return 0.5 * (
                (self.phi(a, sb, ka, kb - 1) if sb else 0.0) +
                (self.phi(a, db, ka, kb - 1) if db else 0.0)
            )
        finally:
            self.stack.discard(key)

    def f5(self, pk):
        s, d = self.parents(pk)
        return self.phi(s, d, 4, 4) if s and d else 0.0

    def cache_info(self):
        return self._phi_cached.cache_info()


class RecentStructureCache:
    """
    5代pathを全馬で無制限cacheするとiPhoneで重い。
    同じ親が複数産駒で使われる場合だけcacheする。
    """
    def __init__(self, by, max_cached_parents=50_000):
        self.by = by
        use = Counter()
        for h in by.values():
            if h.sire and h.sire in by:
                use[h.sire] += 1
            if h.dam and h.dam in by:
                use[h.dam] += 1
        self.use = use
        self._cached_paths = lru_cache(maxsize=max_cached_parents)(self._paths_impl)

    def _paths_impl(self, start):
        if not start or start not in self.by:
            return tuple()
        out = []
        stack = [(start, (start,), 0)]
        while stack:
            node, path, d = stack.pop()
            out.append((node, path))
            if d >= 4:
                continue
            h = self.by[node]
            for p in (h.sire, h.dam):
                if p and p in self.by and p not in path:
                    stack.append((p, path + (p,), d + 1))
        return tuple(out)

    def paths(self, start):
        if not start or start not in self.by:
            return tuple()
        if self.use.get(start, 0) >= 2:
            return self._cached_paths(start)
        return self._paths_impl(start)

    def structure(self, pk):
        h = self.by.get(pk)
        if not h:
            return 0, 0, "", None, None, "NO_RECENT_COMMON"
        ps = {}
        pd = {}
        for anc, path in self.paths(h.sire):
            ps.setdefault(anc, []).append(path)
        for anc, path in self.paths(h.dam):
            pd.setdefault(anc, []).append(path)
        contrib = {}
        pairn = 0
        best = None
        bestlabel = ""
        for anc in set(ps).intersection(pd):
            total = 0.0
            for a in ps[anc]:
                for b in pd[anc]:
                    if set(a[:-1]).intersection(b[:-1]):
                        continue
                    ds = len(a) - 1
                    dd = len(b) - 1
                    total += 0.5 ** (ds + dd + 1)
                    pairn += 1
                    lo, hi = sorted((ds + 1, dd + 1))
                    kk = (lo + hi, hi, lo)
                    if best is None or kk < best:
                        best = kk
                        bestlabel = f"{lo}x{hi}"
            if total > 0:
                contrib[anc] = total
        total = sum(contrib.values())
        if total <= 0:
            return 0, pairn, bestlabel, None, None, "NO_RECENT_COMMON"
        shares = [v / total for v in contrib.values()]
        hhi = sum(x * x for x in shares)
        mx = max(shares)
        if len(shares) == 1:
            cl = "SINGLE_SOURCE"
        elif mx >= 0.60:
            cl = "DOMINANT_SOURCE"
        else:
            cl = "DISTRIBUTED_MULTI_SOURCE"
        return len(shares), pairn, bestlabel, hhi, mx, cl


def fast_compute_all_exact(by, con):
    cached = {pk: f for pk, f in con.execute("SELECT pk,fall FROM exact WHERE fall IS NOT NULL")}
    pre = {pk: float(f) / 100.0 for pk, f in cached.items()}
    todo = [pk for pk in by if pk not in cached]
    if not todo:
        print("Exact F/F5 cache: 全頭再利用")
        return

    print(f"Exact F/F5/5代構造 新規計算: {len(todo):,}頭")
    print("  v1.1: kinship LRU / F5 LRU / 再利用親path cache")
    eng = FastExactFEngine(by, pre_f=pre)
    f5e = FastF5Engine(by, eng)
    struct = RecentStructureCache(by)

    # depth順を第一にして祖先側を先に済ませる。
    todo.sort(key=lambda pk: (eng.depth(pk), by[pk].year if by[pk].year is not None else 9999, pk))
    sql = "INSERT OR REPLACE INTO exact VALUES(?,?,?,?,?,?,?,?,?)"
    batch = []
    t0 = time.time()
    for i, pk in enumerate(todo, 1):
        fall = eng.f(pk) * 100.0
        f5 = f5e.f5(pk) * 100.0
        cn, pn, nc, hhi, mx, cl = struct.structure(pk)
        batch.append((pk, fall, f5, cn, pn, nc, hhi, mx, cl))
        if len(batch) >= 1000:
            con.executemany(sql, batch)
            con.commit()
            batch.clear()
        if i % 5000 == 0:
            ki = eng.cache_info()
            fi = f5e.cache_info()
            print(
                f"  {i:,}/{len(todo):,}  {time.time()-t0:.1f}s  "
                f"kin hit={ki.hits:,} miss={ki.misses:,} size={ki.currsize:,}  "
                f"F5 hit={fi.hits:,} size={fi.currsize:,}"
            )
    if batch:
        con.executemany(sql, batch)
        con.commit()


# ---------- stakes normalized persistent cache ----------

def _stakes_cache_open(paths):
    p = STAKES_CACHE_DB
    con = sqlite3.connect(str(p))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS source_horse(pk TEXT PRIMARY KEY)")
    con.execute("""CREATE TABLE IF NOT EXISTS race(
        pk TEXT,
        country TEXT,
        venue TEXT,
        race_year INTEGER,
        place INTEGER,
        grade TEXT,
        grade_present INTEGER,
        surface TEXT,
        dist_band TEXT,
        age INTEGER
    )""")
    sig = base.file_sig(paths)
    old = con.execute("SELECT v FROM meta WHERE k='stakes_sig'").fetchone()
    if old and old[0] == sig:
        n = con.execute("SELECT COUNT(*) FROM race").fetchone()[0]
        print(f"stakes normalized cache: 再利用 ({n:,} records)")
        return con, True

    print("stakes normalized cache: 初回/更新。JSONを一度だけ正規化します")
    con.execute("DELETE FROM source_horse")
    con.execute("DELETE FROM race")
    con.execute("DELETE FROM meta")
    con.execute("INSERT INTO meta(k,v) VALUES('stakes_sig',?)", (sig,))
    con.commit()

    csv.field_size_limit(sys.maxsize)
    horse_batch = []
    race_batch = []
    total = 0
    kept = 0
    for path in paths:
        print("  normalize:", Path(path).name)
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            pkc = base.pick(r.fieldnames or [], "PrimaryKey", "PK", "HorsePK")
            jc = base.pick(r.fieldnames or [], "RaceDataJSON", "race_data_json", "RaceJSON", "races")
            if not pkc or not jc:
                raise ValueError(f"{Path(path).name}: PrimaryKey/RaceDataJSON列なし")
            for row in r:
                pk = str(row.get(pkc) or "").strip()
                raw = row.get(jc)
                if not pk:
                    continue
                horse_batch.append((pk,))
                if len(horse_batch) >= 5000:
                    con.executemany("INSERT OR IGNORE INTO source_horse(pk) VALUES(?)", horse_batch)
                    horse_batch.clear()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                for rec in base.iter_races(obj):
                    total += 1
                    if base.is_jump(rec):
                        continue
                    pl = base.place(rec)
                    if pl not in (1, 2, 3):
                        continue
                    country = base.norm_country(rec.get("country"))
                    venue = base.japan_venue(rec) if country == "JPN" else ""
                    ry = base.race_year(rec)
                    gn = base.grade_norm(rec)
                    gp = 1 if str(base.getv(rec, "grade", "race_grade", "group_grade", "class_grade") or "").strip() else 0
                    sf = base.surface(rec)
                    dk = base.dist_key(base.distance_m(rec))
                    age = base.iint(base.getv(rec, "age", "horse_age", "age_at_race"))
                    if age is not None and not (1 <= age <= 20):
                        age = None
                    race_batch.append((pk, country, venue, ry, pl, gn, gp, sf, dk, age))
                    kept += 1
                    if len(race_batch) >= 10000:
                        con.executemany("INSERT INTO race VALUES(?,?,?,?,?,?,?,?,?,?)", race_batch)
                        con.commit()
                        race_batch.clear()
                if kept and kept % 100000 == 0:
                    print(f"    normalized top3={kept:,}")
    if horse_batch:
        con.executemany("INSERT OR IGNORE INTO source_horse(pk) VALUES(?)", horse_batch)
    if race_batch:
        con.executemany("INSERT INTO race VALUES(?,?,?,?,?,?,?,?,?,?)", race_batch)
    con.execute("CREATE INDEX IF NOT EXISTS idx_race_country_venue ON race(country,venue)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_race_pk ON race(pk)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_race_year ON race(race_year)")
    con.commit()
    print(f"stakes normalized cache: 完了 top3={kept:,} / raw={total:,}")
    return con, False


def _scope_where(codes, js):
    if not codes:
        return "1=1", []
    if codes == ["JPN"]:
        if js == "JRA":
            return "country=? AND venue='JRA'", ["JPN"]
        if js == "NAR":
            return "country=? AND venue='NAR'", ["JPN"]
        return "country=?", ["JPN"]
    ph = ",".join("?" for _ in codes)
    return f"country IN ({ph})", list(codes)


def fast_parse_stakes_to_db(paths, con, codes, js):
    sc, hit = _stakes_cache_open(paths)
    sc.commit()
    sc_path = Path(sc.execute("PRAGMA database_list").fetchone()[2])
    sc.close()

    con.execute("DROP TABLE IF EXISTS stakes_tmp")
    con.execute("""CREATE TABLE stakes_tmp(
      pk TEXT PRIMARY KEY,row_present INTEGER,top3_n INTEGER,win_n INTEGER,grade_obs INTEGER,
      g1 INTEGER,g1w INTEGER,g2 INTEGER,g2w INTEGER,g3 INTEGER,g3w INTEGER,listed INTEGER,
      multi_year INTEGER,final_age INTEGER,a2 INTEGER,a3 INTEGER,a4 INTEGER,a5 INTEGER,a6 INTEGER,
      turf INTEGER,turfw INTEGER,dirt INTEGER,dirtw INTEGER,
      d1 INTEGER,d1w INTEGER,d2 INTEGER,d2w INTEGER,d3 INTEGER,d3w INTEGER,d4 INTEGER,d4w INTEGER,d5 INTEGER,d5w INTEGER)""")

    clause, params = _scope_where(codes, js)
    gs = base.grade_start_for_scope(codes, js)
    grade_ok = "0" if gs is None else f"CASE WHEN r.race_year>={int(gs)} THEN 1 ELSE 0 END"

    con.execute("ATTACH DATABASE ? AS stc", (str(sc_path),))
    sql = f"""
      INSERT INTO stakes_tmp
      SELECT
        r.pk,
        1,
        COUNT(*),
        SUM(CASE WHEN r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade_present=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G1' THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G1' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G2' THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G2' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G3' THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='G3' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN ({grade_ok})=1 AND r.grade='LISTED' THEN 1 ELSE 0 END),
        CASE WHEN COUNT(DISTINCT r.race_year)>=2 THEN 1 ELSE 0 END,
        MAX(CASE
              WHEN r.age BETWEEN 1 AND 20 THEN r.age
              WHEN r.race_year IS NOT NULL AND h.birth_year IS NOT NULL
                   AND r.race_year-h.birth_year BETWEEN 1 AND 20 THEN r.race_year-h.birth_year
            END),
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=2 THEN 1 ELSE 0 END),
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=3 THEN 1 ELSE 0 END),
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=4 THEN 1 ELSE 0 END),
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)=5 THEN 1 ELSE 0 END),
        MAX(CASE WHEN COALESCE(r.age,r.race_year-h.birth_year)>=6 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.surface='TURF' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.surface='TURF' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.surface='DIRT' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.surface='DIRT' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D1000_1300' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D1000_1300' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D1400_1600' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D1400_1600' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D1700_2000' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D1700_2000' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D2100_2400' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D2100_2400' AND r.place=1 THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D2500P' THEN 1 ELSE 0 END),
        MAX(CASE WHEN r.dist_band='D2500P' AND r.place=1 THEN 1 ELSE 0 END)
      FROM stc.race r
      LEFT JOIN horse h ON h.pk=r.pk
      WHERE {clause}
      GROUP BY r.pk
    """
    con.execute(sql, params)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM stakes_tmp").fetchone()[0]
    rec = con.execute("SELECT COALESCE(SUM(top3_n),0) FROM stakes_tmp").fetchone()[0]
    con.execute("DETACH DATABASE stc")
    print(f"scope平地Top3: {n:,}頭 / {int(rec):,}件  (normalized cache {'HIT' if hit else 'BUILT'})")


# ---------- grouped logistic ----------

def _inv3(m):
    a,b,c=m[0]; d,e,f=m[1]; g,h,i=m[2]
    A=e*i-f*h; B=-(d*i-f*g); C=d*h-e*g
    D=-(b*i-c*h); E=a*i-c*g; F=-(a*h-b*g)
    G=b*f-c*e; H=-(a*f-c*d); I=a*e-b*d
    det=a*A+b*B+c*C
    if abs(det)<1e-14:
        return None
    return [[A/det,D/det,G/det],[B/det,E/det,H/det],[C/det,F/det,I/det]]


def _fit_grouped(groups):
    # groups=(z,n,y,offset)
    beta=[0.0,0.0,0.0]
    H=None
    for _ in range(60):
        grad=[0.0,0.0,0.0]
        H=[[0.0]*3 for _ in range(3)]
        for x,n,y,off in groups:
            zz=(1.0,x,x*x)
            eta=off+sum(beta[j]*zz[j] for j in range(3))
            eta=max(-35.0,min(35.0,eta))
            p=1.0/(1.0+math.exp(-eta))
            rr=y-n*p
            ww=max(1e-12,n*p*(1-p))
            for j in range(3):
                grad[j]+=zz[j]*rr
                for k in range(3):
                    H[j][k]+=zz[j]*zz[k]*ww
        iv=_inv3(H)
        if iv is None:
            return None
        step=[sum(iv[j][k]*grad[k] for k in range(3)) for j in range(3)]
        beta=[beta[j]+step[j] for j in range(3)]
        if max(abs(x) for x in step)<1e-8:
            break
    iv=_inv3(H)
    if iv is None:
        return None
    se=[math.sqrt(max(0.0,iv[j][j])) for j in range(3)]
    vertex=-beta[1]/(2*beta[2]) if abs(beta[2])>1e-12 else None
    return beta,se,vertex


def _rank_col(metric):
    # v1.0 add_rank_tablesの列名規約
    return metric+"Pct"


def _grouped_model_data(con, metric, outcome, where):
    pct=_rank_col(metric)
    # ranked_allはv1.0が作る全blood相対順位table。
    stats={}
    for b5,n,m,m2 in con.execute(
        f"SELECT birth5,COUNT({metric}),AVG({metric}),AVG({metric}*{metric}) "
        f"FROM ranked_all WHERE {where} AND {metric} IS NOT NULL AND birth5 IS NOT NULL GROUP BY birth5"
    ):
        if not n or m is None:
            continue
        var=max(0.0,float(m2)-float(m)*float(m))
        sd=math.sqrt(var)
        stats[b5]=(float(m),sd)
    baseline={}
    for b5,n,y in con.execute(
        f"SELECT birth5,COUNT(*),SUM({outcome}) FROM ranked_all "
        f"WHERE {where} AND {outcome} IS NOT NULL AND birth5 IS NOT NULL GROUP BY birth5"
    ):
        if n and y is not None and 0<y<n:
            p=(float(y)+0.5)/(float(n)+1.0)
            baseline[b5]=math.log(p/(1-p))
    groups=[]
    q=(
        f"SELECT birth5,CAST(MIN(49,CAST({pct}*50 AS INTEGER)) AS INTEGER),"
        f"COUNT(*),AVG({metric}),SUM({outcome}) "
        f"FROM ranked_all WHERE {where} AND {metric} IS NOT NULL AND {pct} IS NOT NULL "
        f"AND {outcome} IS NOT NULL AND birth5 IS NOT NULL "
        f"GROUP BY birth5,CAST(MIN(49,CAST({pct}*50 AS INTEGER)) AS INTEGER)"
    )
    total=0; succ=0
    for b5,cell,n,mv,y in con.execute(q):
        st=stats.get(b5); off=baseline.get(b5)
        if not st or off is None or st[1]<=0 or mv is None:
            continue
        z=(float(mv)-st[0])/st[1]
        groups.append((z,int(n),int(y or 0),off))
        total+=int(n); succ+=int(y or 0)
    return groups,total,succ


def fast_write_logit(con, out):
    metrics=("Fall","F5","DeepDelta","RecentRatio")
    outcomes=("ScopeFlatStakesTop3","ScopeFlatStakesWin","RepeatWin2","RepeatWin5","Top3_5plus","Mature5plus","MultiYearTop3","G1Reach","G1Win","G2Reach","G3Reach")
    fields=["Population","Metric","Outcome","N","SuccessN","BetaZ","SEZ","PZ","BetaZ2","SEZ2","PZ2","VertexSD","GroupedCells","Note"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for pop,where in (("ALL_BLOOD","AnalysisBirthYearEligible=1"),("STAKES_TOP3","AnalysisBirthYearEligible=1 AND ScopeFlatStakesTop3=1")):
            for metric in metrics:
                for outcome in outcomes:
                    groups,n,s=_grouped_model_data(con,metric,outcome,where)
                    if len(groups)<5 or n<=0 or s<=0 or s>=n:
                        continue
                    fit=_fit_grouped(groups)
                    if not fit:
                        continue
                    b,se,v=fit
                    p1=math.erfc(abs(b[1]/se[1])/math.sqrt(2)) if se[1] else None
                    p2=math.erfc(abs(b[2]/se[2])/math.sqrt(2)) if se[2] else None
                    w.writerow({
                        "Population":pop,"Metric":metric,"Outcome":outcome,"N":n,"SuccessN":s,
                        "BetaZ":b[1],"SEZ":se[1],"PZ":p1,"BetaZ2":b[2],"SEZ2":se[2],"PZ2":p2,
                        "VertexSD":v,"GroupedCells":len(groups),
                        "Note":"出生5年帯×F順位50cellへ集約した高速感度モデル。Vertexは最適近交値ではない"
                    })


# v1.0の重い処理を差し替え
base.compute_all_exact = fast_compute_all_exact
base.parse_stakes_to_db = fast_parse_stakes_to_db
base.write_logit = fast_write_logit


def self_test():
    # v1.0の既知ケース + fast engineを別途検証
    by={
        "A":base.Horse("A"),"B":base.Horse("B"),"C":base.Horse("C"),
        "H1":base.Horse("H1",sire="A",dam="B"),"H2":base.Horse("H2",sire="A",dam="C"),"X":base.Horse("X",sire="H1",dam="H2"),
        "F1":base.Horse("F1",sire="A",dam="B"),"F2":base.Horse("F2",sire="A",dam="B"),"Y":base.Horse("Y",sire="F1",dam="F2"),
        "P":base.Horse("P",sire="A",dam="B"),"Z":base.Horse("Z",sire="A",dam="P")
    }
    e=FastExactFEngine(by,max_pairs=10000)
    f5=FastF5Engine(by,e,max_pairs=10000)
    for pk,expect in (("X",.125),("Y",.25),("Z",.25)):
        assert abs(e.f(pk)-expect)<1e-10,(pk,e.f(pk),expect)
        assert abs(f5.f5(pk)-expect)<1e-10,(pk,f5.f5(pk),expect)
    assert base.grade_start_for_scope(["JPN"],"JRA")==1984
    assert base.grade_start_for_scope(["JPN"],"NAR")==1997
    print("FAST SELF TEST OK: half-sib=12.5%, full-sib=25%, parent-offspring=25%, JRA/NAR OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        print("="*76)
        print("高速化レイヤー:", VERSION)
        print("Exact F LRU + F5 LRU + stakes normalized persistent cache + grouped logistic")
        print("="*76)
        base.main()
