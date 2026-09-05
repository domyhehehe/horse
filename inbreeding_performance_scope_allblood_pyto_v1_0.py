#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbreeding_performance_scope_allblood_pyto_v1_0.py

Pyto向け「近交係数 × 競走成績」多軸監査。

設計原則
--------
* 必須入力は blood.csv と stakes_horses*.csv だけ。
* all_horse_inbreeding_exact*.csv 等の事前計算済みExact Fは要求しない。
* 全世代Exact F (Fall) と厳密5代F (F5) を blood.csv から内部計算する。
* blood.csv の馬は stakes DB にいなくても全頭CSVへ出す。
* 同時に、選択scope内の平地stakes Top3経験馬だけのCSV/解析も出す。
* 日本は JPN_ALL / JRA / NAR を切替可能。
* Grade制度以前を G1/G2/G3=0 としない。制度開始以前は NA。
* 人数による除外はしない。小NセルもNを表示して残す。

全blood母集団での stakes 未登場馬は、
  StakesRowPresent=0 / ScopeFlatStakesTop3=0
として保存する。これは「出走して負けた」ではなく
「stakes DBにscope内Top3記録がない」という意味。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

VERSION = "1.0.0-zero-based-all-blood"
sys.setrecursionlimit(20000)

SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
CACHE_DB = SCRIPT_DIR / ".inbreeding_allblood_exact_cache_v1.sqlite3"

COUNTRY_MENU = (
    ("JPN", "日本"), ("USA", "米国"), ("GB", "英国"), ("IRE", "アイルランド"),
    ("FR", "フランス"), ("AUS", "豪州"), ("CAN", "カナダ"), ("GER", "ドイツ"),
    ("NZ", "ニュージーランド"), ("UAE", "UAE"), ("HK", "香港"),
    ("SAF", "南アフリカ"), ("ARG", "アルゼンチン"), ("BRZ", "ブラジル"),
    ("TURKIYE", "トルコ"), ("CHI", "チリ"), ("INDIA", "インド"), ("KOR", "韓国"),
)

COUNTRY_ALIAS = {
    "JAPAN":"JPN","JPN":"JPN","日本":"JPN",
    "USA":"USA","US":"USA","UNITED STATES":"USA","UNITED STATES OF AMERICA":"USA","米国":"USA","アメリカ":"USA",
    "GB":"GB","GBR":"GB","UK":"GB","UNITED KINGDOM":"GB","ENGLAND":"GB","英国":"GB","イギリス":"GB",
    "IRE":"IRE","IRELAND":"IRE","アイルランド":"IRE",
    "FR":"FR","FRA":"FR","FRANCE":"FR","フランス":"FR",
    "AUS":"AUS","AUSTRALIA":"AUS","豪州":"AUS","オーストラリア":"AUS",
    "CAN":"CAN","CANADA":"CAN","カナダ":"CAN",
    "GER":"GER","DE":"GER","GERMANY":"GER","ドイツ":"GER",
    "NZ":"NZ","NEW ZEALAND":"NZ","ニュージーランド":"NZ",
    "UAE":"UAE","UNITED ARAB EMIRATES":"UAE",
    "HK":"HK","HKG":"HK","HONG KONG":"HK","香港":"HK",
    "SAF":"SAF","SOUTH AFRICA":"SAF","南アフリカ":"SAF",
    "ARG":"ARG","ARGENTINA":"ARG","アルゼンチン":"ARG",
    "BRZ":"BRZ","BRAZIL":"BRZ","ブラジル":"BRZ",
    "TURKIYE":"TURKIYE","TURKEY":"TURKIYE","トルコ":"TURKIYE",
    "CHI":"CHI","CHILE":"CHI","チリ":"CHI",
    "INDIA":"INDIA","IND":"INDIA","インド":"INDIA",
    "KOR":"KOR","KOREA":"KOR","SOUTH KOREA":"KOR","韓国":"KOR",
}

JRA_TRACKS = {
    "SAPPORO","札幌","HAKODATE","函館","FUKUSHIMA","FUKUCHIMA","福島",
    "NIIGATA","新潟","TOKYO","TOKYO FUCHU","TOKYO RACECOURSE","FUCHU","FUCHUU","東京","府中",
    "NAKAYAMA","NAKAYAMA RACECOURSE","中山","CHUKYO","中京","KYOTO","京都",
    "HANSHIN","HANSHIN CHUKYO","阪神","KOKURA","小倉",
    "TOKYO MEGURO","MEGURO TOKYO","MEGURO","目黒","NARUO","鳴尾",
}

# 史実として開始年を明確に置けるscopeだけを使う。
# JPN_ALLはJRA/NAR双方で格付けを比較可能にするため保守的に1997。
GRADE_START = {
    "JPN_JRA": 1984,
    "JPN_NAR": 1997,
    "JPN_ALL": 1997,
    "USA": 1973,
    "CAN": 1973,
    "GB": 1971,
    "IRE": 1971,
    "FR": 1971,
}

JUMP_PATTERN = re.compile(
    r"(?:\bHURDLES?\b|\bJUMPS?\b|\bSTEEPLE(?:CHASE)?S?\b|\bNOVICES?\s+CHASE\b|"
    r"\bHANDICAP\s+CHASE\b|\bCHAMPION\s+CHASE\b|\bCELEBRATION\s+CHASE\b|"
    r"\bCHASE\b|\bNATIONAL\s+HUNT\b|\bNH\s+FLAT\b|\bPOINT\s+TO\s+POINT\b|"
    r"\bCROSS\s+COUNTRY\b|障害|ハードル|スティープルチェイス)", re.I,
)

DIST_BANDS = (
    ("D1000_1300", 0, 1300),
    ("D1400_1600", 1300, 1600),
    ("D1700_2000", 1600, 2000),
    ("D2100_2400", 2000, 2400),
    ("D2500P", 2400, 100000),
)

F_BANDS = (
    (0.00,0.01,"00-01%"),(0.01,0.05,"01-05%"),(0.05,0.10,"05-10%"),
    (0.10,0.25,"10-25%"),(0.25,0.75,"25-75%"),(0.75,0.90,"75-90%"),
    (0.90,0.95,"90-95%"),(0.95,0.99,"95-99%"),(0.99,1.000001,"99-100%"),
)


def nt(v):
    s = unicodedata.normalize("NFKC", str(v or "")).upper().strip()
    return re.sub(r"[\s\u3000]+", " ", s)


def norm_country(v):
    s = nt(v)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^0-9A-Z\u3040-\u30ff\u3400-\u9fff]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return COUNTRY_ALIAS.get(s, s)


def norm_track(v):
    s = nt(v)
    s = re.sub(r"[^0-9A-Z\u3040-\u30ff\u3400-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def iint(v):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    try: return int(float(s))
    except Exception:
        m = re.search(r"(17|18|19|20)\d{2}", s)
        return int(m.group()) if m else None


def ffloat(v):
    try: return float(str(v).strip())
    except Exception: return None


def pick(fields, *aliases):
    mp = {nt(x): x for x in fields if x}
    for a in aliases:
        if nt(a) in mp: return mp[nt(a)]
    return None


def file_sig(paths):
    h = hashlib.sha256()
    for p in sorted([Path(x) for x in paths], key=lambda x: str(x)):
        st = p.stat()
        h.update(str(p.resolve()).encode("utf-8", "ignore"))
        h.update(str(st.st_size).encode())
        h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()


@dataclass
class Horse:
    pk: str
    name: str = ""
    year: int | None = None
    country: str = ""
    sex: str = ""
    sire: str = ""
    dam: str = ""


def discover_inputs():
    roots = []
    for r in (SCRIPT_DIR, Path.cwd()):
        rr = r.resolve()
        if rr not in roots: roots.append(rr)
    bloods = []
    for r in roots: bloods += list(r.glob("blood*.csv"))
    bloods = sorted(set(p for p in bloods if p.is_file()), key=lambda p: (p.name != "blood.csv", p.name))
    if bloods:
        print("\nblood候補")
        for i,p in enumerate(bloods,1): print(f" {i}: {p}")
        raw = input("blood [空欄=1]: ").strip()
        blood = bloods[int(raw)-1] if raw.isdigit() else (bloods[0] if not raw else Path(raw).expanduser())
    else:
        blood = Path(input("blood.csv のパス: ").strip()).expanduser()
    if not blood.is_file(): raise SystemExit(f"bloodがありません: {blood}")

    stakes = []
    for r in (blood.parent, *roots):
        stakes += list(r.glob("stakes_horses*.csv"))
        stakes += list(r.glob("*stakes_horses*.csv"))
    stakes = sorted(set(p for p in stakes if p.is_file()))
    if stakes:
        print("\nstakes候補（Enterで全部使用）")
        for p in stakes: print(" ",p)
        raw = input("別指定する場合だけ ; 区切りで入力: ").strip()
        if raw: stakes = [Path(x.strip()).expanduser() for x in raw.split(";") if x.strip()]
    else:
        raw = input("stakes CSVパス（複数は ; 区切り）: ").strip()
        stakes = [Path(x.strip()).expanduser() for x in raw.split(";") if x.strip()]
    if not stakes or not all(p.is_file() for p in stakes): raise SystemExit("stakes CSVを確認できません")
    return blood, stakes


def choose_scope():
    print("\n対象レース開催国")
    print("  0 : ALL / 全世界")
    for i,(c,l) in enumerate(COUNTRY_MENU,1): print(f" {i:>2} : {l} ({c})")
    print(" 99 : 国コード/国名を直接入力（複数はカンマ区切り）")
    lookup = {str(i): c for i,(c,_l) in enumerate(COUNTRY_MENU,1)}
    while True:
        raw = input("開催国 [空欄/0=ALL、1=日本、2=米国]: ").strip()
        if not raw or raw == "0": return None, "ALL"
        if raw == "99":
            custom = input("国コード/国名 [例 JPN,USA]: ").strip()
            vals = [norm_country(x) for x in custom.split(",") if norm_country(x)]
        else:
            vals = []
            bad = False
            for x in [z.strip() for z in raw.split(",") if z.strip()]:
                if x not in lookup: bad = True; break
                vals.append(lookup[x])
            if bad:
                print("番号を確認してください"); continue
        vals = list(dict.fromkeys(vals))
        if not vals: continue
        if vals == ["JPN"]:
            print("\n日本開催の範囲")
            print("  1 : JPN_ALL（中央＋地方＋track不明）")
            print("  2 : JRA（中央）")
            print("  3 : NAR（地方）")
            js = input("日本開催範囲 [空欄/1=JPN_ALL]: ").strip() or "1"
            js = {"1":"ALL","2":"JRA","3":"NAR"}.get(js, "ALL")
            return vals, js
        return vals, "ALL"


def choose_years():
    raw = input("対象馬出生年 [空欄=全期間 / 例 1990-2018]: ").strip()
    if not raw: return None, None
    m = re.match(r"^\s*(\d{4})\s*[-~〜]\s*(\d{4})\s*$", raw)
    if not m:
        print("形式を認識できないため全期間にします")
        return None, None
    a,b = map(int,m.groups())
    return min(a,b), max(a,b)


def scope_label(codes, js):
    if not codes: return "ALL"
    if codes == ["JPN"]: return "JPN_ALL" if js == "ALL" else js
    return ",".join(codes)


def japan_venue(rec):
    if norm_country(rec.get("country")) != "JPN": return ""
    tr = norm_track(rec.get("track"))
    if not tr: return "UNKNOWN"
    return "JRA" if tr in JRA_TRACKS else "NAR"


def race_allowed(rec, codes, js):
    if codes and norm_country(rec.get("country")) not in set(codes): return False
    if codes == ["JPN"] and js in {"JRA","NAR"}:
        return japan_venue(rec) == js
    return True


def grade_start_for_scope(codes, js):
    if codes == ["JPN"]:
        return GRADE_START["JPN_ALL" if js == "ALL" else f"JPN_{js}"]
    if codes and len(codes) == 1:
        return GRADE_START.get(codes[0])
    return None


def grade_allowed_race(rec, codes, js):
    y = race_year(rec)
    if y is None: return False
    if codes == ["JPN"]:
        venue = japan_venue(rec)
        if venue == "JRA": return y >= GRADE_START["JPN_JRA"]
        if venue == "NAR": return y >= GRADE_START["JPN_NAR"]
        return y >= GRADE_START["JPN_ALL"]
    if codes and len(codes) == 1:
        st = GRADE_START.get(codes[0])
        return st is not None and y >= st
    # ALL/複数国では国ごとに判定。未知国はGrade比較へ自動投入しない。
    c = norm_country(rec.get("country"))
    st = GRADE_START.get(c)
    return st is not None and y >= st


def load_blood(path):
    last = None
    for enc in ("utf-8-sig","cp932"):
        by = {}
        try:
            with open(path,"r",encoding=enc,newline="") as f:
                r = csv.DictReader(f)
                if not r.fieldnames: raise ValueError("bloodヘッダなし")
                pkc = pick(r.fieldnames,"PrimaryKey","PK","HorsePK")
                nc = pick(r.fieldnames,"Horse Name","HorseName","Name")
                yc = pick(r.fieldnames,"Year","BirthYear")
                cc = pick(r.fieldnames,"Country","BirthCountry")
                sexc = pick(r.fieldnames,"Sex")
                sc = pick(r.fieldnames,"Sire","SirePK","Father","FatherPK")
                dc = pick(r.fieldnames,"Dam","DamPK","Mother","MotherPK")
                if not pkc or not sc or not dc: raise ValueError("PrimaryKey/Sire/Dam列を確認できません")
                for row in r:
                    pk = str(row.get(pkc) or "").strip()
                    if not pk: continue
                    by[pk] = Horse(
                        pk=pk,
                        name=str(row.get(nc) or pk).strip() if nc else pk,
                        year=iint(row.get(yc)) if yc else None,
                        country=norm_country(row.get(cc)) if cc else "",
                        sex=str(row.get(sexc) or "").strip() if sexc else "",
                        sire=str(row.get(sc) or "").strip(),
                        dam=str(row.get(dc) or "").strip(),
                    )
            print(f"blood読込: {len(by):,}頭 ({enc})")
            return by
        except Exception as e:
            last = e
    raise last


class ExactFEngine:
    def __init__(self, by, pre_f=None, max_pairs=1200000):
        self.by = by
        self.depth_cache = {}
        self.f_cache = dict(pre_f or {})
        self.kin_cache = {}
        self.depth_stack = set(); self.f_stack = set(); self.kin_stack = set()
        self.max_pairs = max_pairs

    def parents(self, pk):
        h = self.by.get(pk)
        if not h: return "",""
        s = h.sire if h.sire and h.sire != pk and h.sire in self.by else ""
        d = h.dam if h.dam and h.dam != pk and h.dam in self.by else ""
        return s,d

    def depth(self, pk):
        if not pk or pk not in self.by: return 0
        if pk in self.depth_cache: return self.depth_cache[pk]
        if pk in self.depth_stack: return 0
        self.depth_stack.add(pk)
        s,d = self.parents(pk)
        vals = [self.depth(x) for x in (s,d) if x]
        z = 1 + max(vals) if vals else 0
        self.depth_stack.discard(pk)
        self.depth_cache[pk] = z
        return z

    def f(self, pk):
        if pk in self.f_cache: return self.f_cache[pk]
        if pk in self.f_stack: return 0.0
        self.f_stack.add(pk)
        s,d = self.parents(pk)
        z = self.kin(s,d) if s and d else 0.0
        self.f_stack.discard(pk)
        self.f_cache[pk] = z
        return z

    def _which_expand(self, a,b):
        sa,da = self.parents(a); sb,db = self.parents(b)
        ha = bool(sa or da); hb = bool(sb or db)
        if ha and not hb: return 0
        if hb and not ha: return 1
        if not ha and not hb: return -1
        depa,depb = self.depth(a),self.depth(b)
        if depa != depb: return 0 if depa > depb else 1
        ya = self.by[a].year; yb = self.by[b].year
        if ya is not None and yb is not None and ya != yb: return 0 if ya > yb else 1
        return 0 if a > b else 1

    def kin(self,a,b):
        if not a or not b or a not in self.by or b not in self.by: return 0.0
        key = (a,b) if a <= b else (b,a)
        if key in self.kin_cache: return self.kin_cache[key]
        if key in self.kin_stack: return 0.0
        self.kin_stack.add(key)
        try:
            if a == b:
                z = 0.5 * (1.0 + self.f(a))
            else:
                side = self._which_expand(a,b)
                if side < 0:
                    z = 0.0
                elif side == 0:
                    s,d = self.parents(a)
                    z = 0.5 * ((self.kin(s,b) if s else 0.0) + (self.kin(d,b) if d else 0.0))
                else:
                    s,d = self.parents(b)
                    z = 0.5 * ((self.kin(a,s) if s else 0.0) + (self.kin(a,d) if d else 0.0))
            self.kin_cache[key] = z
            if len(self.kin_cache) > self.max_pairs:
                self.kin_cache.clear()
            return z
        finally:
            self.kin_stack.discard(key)


class F5Engine:
    def __init__(self, by, depth_engine):
        self.by = by
        self.depth_engine = depth_engine
        self.memo = {}; self.fm = {}; self.stack = set()

    def parents(self,pk): return self.depth_engine.parents(pk)

    def fnode(self,pk,k):
        if not pk or pk not in self.by or k <= 0: return 0.0
        key=(pk,k)
        if key in self.fm: return self.fm[key]
        s,d=self.parents(pk)
        z=self.phi(s,d,k-1,k-1) if s and d else 0.0
        self.fm[key]=z
        return z

    def phi(self,a,b,ka,kb):
        if not a or not b or a not in self.by or b not in self.by: return 0.0
        key=(a,b,ka,kb) if (a,ka) <= (b,kb) else (b,a,kb,ka)
        if key in self.memo: return self.memo[key]
        if key in self.stack: return 0.0
        self.stack.add(key)
        try:
            if a == b:
                z=0.5*(1+self.fnode(a,max(ka,kb)))
            elif ka<=0 and kb<=0:
                z=0.0
            else:
                sa,da=self.parents(a); sb,db=self.parents(b)
                cana=ka>0 and bool(sa or da); canb=kb>0 and bool(sb or db)
                if cana and not canb: side=0
                elif canb and not cana: side=1
                elif not cana and not canb: side=-1
                else:
                    depa,depb=self.depth_engine.depth(a),self.depth_engine.depth(b)
                    if depa!=depb: side=0 if depa>depb else 1
                    else:
                        ya=self.by[a].year; yb=self.by[b].year
                        side=0 if (ya is not None and yb is not None and ya>yb) else 1
                if side<0: z=0.0
                elif side==0:
                    z=0.5*((self.phi(sa,b,ka-1,kb) if sa else 0.0)+(self.phi(da,b,ka-1,kb) if da else 0.0))
                else:
                    z=0.5*((self.phi(a,sb,ka,kb-1) if sb else 0.0)+(self.phi(a,db,ka,kb-1) if db else 0.0))
            self.memo[key]=z
            return z
        finally:
            self.stack.discard(key)

    def f5(self,pk):
        s,d=self.parents(pk)
        return self.phi(s,d,4,4) if s and d else 0.0

    def trim(self):
        if len(self.memo)>300000: self.memo.clear()
        if len(self.fm)>200000: self.fm.clear()


def ancestor_paths(by,start,max_edges=4):
    out=defaultdict(list)
    if not start or start not in by: return out
    stack=[(start,(start,),0)]
    while stack:
        node,path,d=stack.pop()
        out[node].append(path)
        if d>=max_edges: continue
        h=by[node]
        for p in (h.sire,h.dam):
            if p and p in by and p not in path: stack.append((p,path+(p,),d+1))
    return out


def recent_structure(by,pk):
    h=by.get(pk)
    if not h: return 0,0,"",None,None,"NO_RECENT_COMMON"
    ps=ancestor_paths(by,h.sire,4); pd=ancestor_paths(by,h.dam,4)
    contrib=defaultdict(float); pairn=0; best=None; bestlabel=""
    for anc in set(ps)&set(pd):
        for a in ps[anc]:
            for b in pd[anc]:
                if set(a[:-1]) & set(b[:-1]): continue
                ds=len(a)-1; dd=len(b)-1
                contrib[anc]+=0.5**(ds+dd+1); pairn+=1
                lo,hi=sorted((ds+1,dd+1)); kk=(lo+hi,hi,lo)
                if best is None or kk<best: best=kk; bestlabel=f"{lo}x{hi}"
    total=sum(contrib.values())
    if total<=0: return 0,pairn,bestlabel,None,None,"NO_RECENT_COMMON"
    shares=[v/total for v in contrib.values()]
    hhi=sum(x*x for x in shares); mx=max(shares)
    cl="SINGLE_SOURCE" if len(shares)==1 else ("DOMINANT_SOURCE" if mx>=0.60 else "DISTRIBUTED_MULTI_SOURCE")
    return len(shares),pairn,bestlabel,hhi,mx,cl


def init_exact_cache(path,blood):
    con=sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS exact(
      pk TEXT PRIMARY KEY, fall REAL, f5 REAL, common_n INTEGER, path_pair_n INTEGER,
      nearest_cross TEXT, source_hhi REAL, max_source_share REAL, structure_class TEXT)""")
    sig=file_sig([blood])
    old=con.execute("SELECT v FROM meta WHERE k='blood_sig'").fetchone()
    if not old or old[0]!=sig:
        con.execute("DELETE FROM exact"); con.execute("DELETE FROM meta")
        con.execute("INSERT INTO meta(k,v) VALUES('blood_sig',?)",(sig,)); con.commit()
    return con


def compute_all_exact(by,con):
    cached={pk:f for pk,f in con.execute("SELECT pk,fall FROM exact")}
    pre={pk:float(f)/100.0 for pk,f in cached.items() if f is not None}
    todo=[pk for pk in by if pk not in cached]
    if not todo:
        print("Exact F/F5 cache: 全頭再利用")
        return
    print(f"Exact F/F5/5代構造 新規計算: {len(todo):,}頭")
    eng=ExactFEngine(by,pre_f=pre); f5e=F5Engine(by,eng)
    todo.sort(key=lambda pk:(by[pk].year if by[pk].year is not None else 9999, eng.depth(pk), pk))
    batch=[]; t0=time.time()
    sql="INSERT OR REPLACE INTO exact VALUES(?,?,?,?,?,?,?,?,?)"
    for i,pk in enumerate(todo,1):
        fall=eng.f(pk)*100.0; f5=f5e.f5(pk)*100.0
        cn,pn,nc,hhi,mx,cl=recent_structure(by,pk)
        batch.append((pk,fall,f5,cn,pn,nc,hhi,mx,cl))
        if len(batch)>=500:
            con.executemany(sql,batch); con.commit(); batch=[]; f5e.trim()
        if i%5000==0: print(f"  {i:,}/{len(todo):,}  {time.time()-t0:.1f}s")
    if batch: con.executemany(sql,batch); con.commit()


def getv(rec,*keys):
    mp={str(k).casefold():v for k,v in rec.items()}
    for k in keys:
        if k.casefold() in mp: return mp[k.casefold()]
    return None


def iter_races(obj):
    if isinstance(obj,list):
        for x in obj:
            if isinstance(x,dict): yield x
        return
    if isinstance(obj,dict):
        for k in ("races","records","results","race_data","racedata","RaceData"):
            v=obj.get(k)
            if isinstance(v,list):
                for x in v:
                    if isinstance(x,dict): yield x
                return
        yield obj


def place(rec):
    v=getv(rec,"placing","place","finish","position","rank","finish_position")
    m=re.match(r"(\d+)",str(v or "").strip())
    return int(m.group(1)) if m else None


def race_year(rec): return iint(getv(rec,"year","race_year","date","race_date"))


def grade_norm(rec):
    # series_gradeは格付け判定には使わない。
    s=nt(getv(rec,"grade","race_grade","group_grade","class_grade"))
    if not s: return ""
    c=re.sub(r"[._\-\s]+","",s)
    if c in {"G1","GRADE1","GROUP1","GI","GRADEI","GROUPI","JPN1","JPNI"}: return "G1"
    if c in {"G2","GRADE2","GROUP2","GII","GRADEII","GROUPII","JPN2","JPNII"}: return "G2"
    if c in {"G3","GRADE3","GROUP3","GIII","GRADEIII","GROUPIII","JPN3","JPNIII"}: return "G3"
    if "LISTED" in s or c in {"L","LR"}: return "LISTED"
    return "OTHER"


def is_jump(rec):
    text=" ".join(str(getv(rec,k) or "") for k in ("race_name","grade","series_grade","comment"))
    return bool(JUMP_PATTERN.search(text))


def surface(rec):
    s=nt(getv(rec,"surface","track_surface","course_surface"))
    if any(x in s for x in ("SYNTHETIC","SYNTH","ALL WEATHER","POLYTRACK","TAPETA","AWT")): return "SYNTH"
    if any(x in s for x in ("DIRT","SAND","ダート")): return "DIRT"
    if any(x in s for x in ("TURF","GRASS","芝")): return "TURF"
    return ""


def distance_m(rec):
    x=ffloat(getv(rec,"distance_m","distance_meter","distance_metres","meters","metres"))
    if x is not None and 500<=x<=10000: return x
    s=str(getv(rec,"distance","race_distance","dist") or "").lower().replace(",","")
    m=re.search(r"(\d+(?:\.\d+)?)\s*m(?:et(?:er|re)s?)?\b",s)
    if m: return float(m.group(1))
    m=re.search(r"(\d+(?:\.\d+)?)\s*f(?:urlongs?)?\b",s)
    if m: return float(m.group(1))*201.168
    m=re.search(r"(\d+(?:\.\d+)?)\s*mi(?:le|les)?\b",s)
    if m: return float(m.group(1))*1609.344
    x=ffloat(s)
    return x if x is not None and 500<=x<=10000 else None


def dist_key(d):
    if d is None: return ""
    for k,lo,hi in DIST_BANDS:
        if lo<d<=hi: return k
    return ""


def create_work_db(path,by,exact_db):
    if path.exists(): path.unlink()
    con=sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA synchronous=OFF")
    con.execute("""CREATE TABLE horse(
      pk TEXT PRIMARY KEY,name TEXT,birth_year INTEGER,birth_country TEXT,sex TEXT,sire TEXT,dam TEXT,
      Fall REAL,F5 REAL,DeepDelta REAL,RecentRatio REAL,F5CommonAncestorN INTEGER,F5PathPairN INTEGER,
      F5NearestCross TEXT,F5SourceHHI REAL,F5MaxSourceShare REAL,F5StructureClass TEXT,
      StakesRowPresent INTEGER DEFAULT 0,ScopeTop3N INTEGER DEFAULT 0,ScopeWinN INTEGER DEFAULT 0,
      ScopeFlatStakesTop3 INTEGER DEFAULT 0,ScopeFlatStakesWin INTEGER DEFAULT 0,
      GradeEraEligible INTEGER,GradeRecordObserved INTEGER DEFAULT 0,G1Reach INTEGER,G1Win INTEGER,
      G2Reach INTEGER,G2Win INTEGER,G3Reach INTEGER,G3Win INTEGER,ListedReach INTEGER,
      RepeatWin2 INTEGER DEFAULT 0,RepeatWin3 INTEGER DEFAULT 0,RepeatWin5 INTEGER DEFAULT 0,
      Top3_5plus INTEGER DEFAULT 0,Top3_8plus INTEGER DEFAULT 0,MultiYearTop3 INTEGER DEFAULT 0,
      FinalTop3Age INTEGER,Age2Top3 INTEGER DEFAULT 0,Age3Top3 INTEGER DEFAULT 0,Age4Top3 INTEGER DEFAULT 0,
      Age5Top3 INTEGER DEFAULT 0,Age6plusTop3 INTEGER DEFAULT 0,Mature5plus INTEGER DEFAULT 0,
      TurfTop3 INTEGER DEFAULT 0,TurfWin INTEGER DEFAULT 0,DirtTop3 INTEGER DEFAULT 0,DirtWin INTEGER DEFAULT 0,
      TurfDirtBoth INTEGER DEFAULT 0,D1000_1300Top3 INTEGER DEFAULT 0,D1000_1300Win INTEGER DEFAULT 0,
      D1400_1600Top3 INTEGER DEFAULT 0,D1400_1600Win INTEGER DEFAULT 0,D1700_2000Top3 INTEGER DEFAULT 0,
      D1700_2000Win INTEGER DEFAULT 0,D2100_2400Top3 INTEGER DEFAULT 0,D2100_2400Win INTEGER DEFAULT 0,
      D2500PTop3 INTEGER DEFAULT 0,D2500PWin INTEGER DEFAULT 0,DistanceMulti2 INTEGER DEFAULT 0,
      DistanceMulti3 INTEGER DEFAULT 0,AnalysisBirthYearEligible INTEGER DEFAULT 1,birth5 INTEGER)""")
    con.execute("ATTACH DATABASE ? AS ex",(str(exact_db),))
    sql="INSERT INTO horse(pk,name,birth_year,birth_country,sex,sire,dam,AnalysisBirthYearEligible,birth5) VALUES(?,?,?,?,?,?,?,?,?)"
    batch=[]
    for h in by.values():
        batch.append((h.pk,h.name,h.year,h.country,h.sex,h.sire,h.dam,1,(h.year//5)*5 if h.year else None))
        if len(batch)>=5000: con.executemany(sql,batch); batch=[]
    if batch: con.executemany(sql,batch)
    con.commit()
    con.execute("""UPDATE horse SET
      Fall=(SELECT fall FROM ex.exact e WHERE e.pk=horse.pk),
      F5=(SELECT f5 FROM ex.exact e WHERE e.pk=horse.pk),
      F5CommonAncestorN=(SELECT common_n FROM ex.exact e WHERE e.pk=horse.pk),
      F5PathPairN=(SELECT path_pair_n FROM ex.exact e WHERE e.pk=horse.pk),
      F5NearestCross=(SELECT nearest_cross FROM ex.exact e WHERE e.pk=horse.pk),
      F5SourceHHI=(SELECT source_hhi FROM ex.exact e WHERE e.pk=horse.pk),
      F5MaxSourceShare=(SELECT max_source_share FROM ex.exact e WHERE e.pk=horse.pk),
      F5StructureClass=(SELECT structure_class FROM ex.exact e WHERE e.pk=horse.pk)""")
    con.commit(); con.execute("DETACH DATABASE ex")
    con.execute("UPDATE horse SET DeepDelta=Fall-F5, RecentRatio=CASE WHEN Fall>0 THEN F5/Fall END")
    con.commit(); return con


def parse_stakes_to_db(paths,con,codes,js):
    con.execute("""CREATE TABLE stakes_tmp(
      pk TEXT PRIMARY KEY,row_present INTEGER,top3_n INTEGER,win_n INTEGER,grade_obs INTEGER,
      g1 INTEGER,g1w INTEGER,g2 INTEGER,g2w INTEGER,g3 INTEGER,g3w INTEGER,listed INTEGER,
      multi_year INTEGER,final_age INTEGER,a2 INTEGER,a3 INTEGER,a4 INTEGER,a5 INTEGER,a6 INTEGER,
      turf INTEGER,turfw INTEGER,dirt INTEGER,dirtw INTEGER,
      d1 INTEGER,d1w INTEGER,d2 INTEGER,d2w INTEGER,d3 INTEGER,d3w INTEGER,d4 INTEGER,d4w INTEGER,d5 INTEGER,d5w INTEGER)""")
    sql="""INSERT INTO stakes_tmp VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(pk) DO UPDATE SET
      row_present=1, top3_n=top3_n+excluded.top3_n, win_n=win_n+excluded.win_n,
      grade_obs=MAX(grade_obs,excluded.grade_obs),g1=MAX(g1,excluded.g1),g1w=MAX(g1w,excluded.g1w),
      g2=MAX(g2,excluded.g2),g2w=MAX(g2w,excluded.g2w),g3=MAX(g3,excluded.g3),g3w=MAX(g3w,excluded.g3w),
      listed=MAX(listed,excluded.listed),multi_year=MAX(multi_year,excluded.multi_year),
      final_age=MAX(COALESCE(final_age,0),COALESCE(excluded.final_age,0)),a2=MAX(a2,excluded.a2),a3=MAX(a3,excluded.a3),
      a4=MAX(a4,excluded.a4),a5=MAX(a5,excluded.a5),a6=MAX(a6,excluded.a6),turf=MAX(turf,excluded.turf),
      turfw=MAX(turfw,excluded.turfw),dirt=MAX(dirt,excluded.dirt),dirtw=MAX(dirtw,excluded.dirtw),
      d1=MAX(d1,excluded.d1),d1w=MAX(d1w,excluded.d1w),d2=MAX(d2,excluded.d2),d2w=MAX(d2w,excluded.d2w),
      d3=MAX(d3,excluded.d3),d3w=MAX(d3w,excluded.d3w),d4=MAX(d4,excluded.d4),d4w=MAX(d4w,excluded.d4w),
      d5=MAX(d5,excluded.d5),d5w=MAX(d5w,excluded.d5w)"""
    total=0; kept=0; batch=[]
    csv.field_size_limit(sys.maxsize)
    for path in paths:
        print("stakes読込:",path.name)
        with open(path,"r",encoding="utf-8-sig",newline="") as f:
            r=csv.DictReader(f)
            pkc=pick(r.fieldnames or [],"PrimaryKey","PK","HorsePK")
            jc=pick(r.fieldnames or [],"RaceDataJSON","race_data_json","RaceJSON","races")
            if not pkc or not jc: raise ValueError(f"{path.name}: PrimaryKey/RaceDataJSON列なし")
            for row in r:
                pk=str(row.get(pkc) or "").strip(); raw=row.get(jc)
                if not pk or not raw: continue
                try: obj=json.loads(raw)
                except Exception: continue
                top3=win=gobs=g1=g1w=g2=g2w=g3=g3w=listed=0
                yrs=set(); ages=[]; turf=turfw=dirt=dirtw=0
                ds={k:[0,0] for k,_,_ in DIST_BANDS}
                for rec in iter_races(obj):
                    total+=1
                    if is_jump(rec) or not race_allowed(rec,codes,js): continue
                    p=place(rec)
                    if p not in (1,2,3): continue
                    kept+=1; top3+=1; isw=(p==1); win+=int(isw)
                    ry=race_year(rec)
                    if ry: yrs.add(ry)
                    age=iint(getv(rec,"age","horse_age","age_at_race"))
                    if age and 1<=age<=20: ages.append(age)
                    if grade_allowed_race(rec,codes,js):
                        gn=grade_norm(rec)
                        if gn:
                            gobs=1
                            if gn=="G1": g1=1; g1w=max(g1w,int(isw))
                            elif gn=="G2": g2=1; g2w=max(g2w,int(isw))
                            elif gn=="G3": g3=1; g3w=max(g3w,int(isw))
                            elif gn=="LISTED": listed=1
                    sf=surface(rec)
                    if sf=="TURF": turf=1; turfw=max(turfw,int(isw))
                    elif sf=="DIRT": dirt=1; dirtw=max(dirtw,int(isw))
                    dk=dist_key(distance_m(rec))
                    if dk: ds[dk][0]=1; ds[dk][1]=max(ds[dk][1],int(isw))
                if top3==0: continue
                dvals=[]
                for k,_,_ in DIST_BANDS: dvals += ds[k]
                vals=(pk,1,top3,win,gobs,g1,g1w,g2,g2w,g3,g3w,listed,int(len(yrs)>=2),max(ages) if ages else None,
                      int(2 in ages),int(3 in ages),int(4 in ages),int(5 in ages),int(any(a>=6 for a in ages)),
                      turf,turfw,dirt,dirtw,*dvals)
                batch.append(vals)
                if len(batch)>=3000: con.executemany(sql,batch); con.commit(); batch=[]
    if batch: con.executemany(sql,batch); con.commit()
    print(f"scope平地Top3: {con.execute('SELECT COUNT(*) FROM stakes_tmp').fetchone()[0]:,}頭 / {kept:,}件")


def apply_scope_outcomes(con,codes,js,ystart,yend):
    gs=grade_start_for_scope(codes,js)
    if ystart is not None:
        con.execute("UPDATE horse SET AnalysisBirthYearEligible=CASE WHEN birth_year BETWEEN ? AND ? THEN 1 ELSE 0 END",(ystart,yend))
    con.execute("""UPDATE horse SET
      StakesRowPresent=COALESCE((SELECT row_present FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      ScopeTop3N=COALESCE((SELECT top3_n FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      ScopeWinN=COALESCE((SELECT win_n FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      ScopeFlatStakesTop3=CASE WHEN COALESCE((SELECT top3_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>0 THEN 1 ELSE 0 END,
      ScopeFlatStakesWin=CASE WHEN COALESCE((SELECT win_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>0 THEN 1 ELSE 0 END,
      GradeRecordObserved=COALESCE((SELECT grade_obs FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      RepeatWin2=CASE WHEN COALESCE((SELECT win_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>=2 THEN 1 ELSE 0 END,
      RepeatWin3=CASE WHEN COALESCE((SELECT win_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>=3 THEN 1 ELSE 0 END,
      RepeatWin5=CASE WHEN COALESCE((SELECT win_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>=5 THEN 1 ELSE 0 END,
      Top3_5plus=CASE WHEN COALESCE((SELECT top3_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>=5 THEN 1 ELSE 0 END,
      Top3_8plus=CASE WHEN COALESCE((SELECT top3_n FROM stakes_tmp s WHERE s.pk=horse.pk),0)>=8 THEN 1 ELSE 0 END,
      MultiYearTop3=COALESCE((SELECT multi_year FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      FinalTop3Age=(SELECT final_age FROM stakes_tmp s WHERE s.pk=horse.pk),
      Age2Top3=COALESCE((SELECT a2 FROM stakes_tmp s WHERE s.pk=horse.pk),0),Age3Top3=COALESCE((SELECT a3 FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      Age4Top3=COALESCE((SELECT a4 FROM stakes_tmp s WHERE s.pk=horse.pk),0),Age5Top3=COALESCE((SELECT a5 FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      Age6plusTop3=COALESCE((SELECT a6 FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      TurfTop3=COALESCE((SELECT turf FROM stakes_tmp s WHERE s.pk=horse.pk),0),TurfWin=COALESCE((SELECT turfw FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      DirtTop3=COALESCE((SELECT dirt FROM stakes_tmp s WHERE s.pk=horse.pk),0),DirtWin=COALESCE((SELECT dirtw FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      D1000_1300Top3=COALESCE((SELECT d1 FROM stakes_tmp s WHERE s.pk=horse.pk),0),D1000_1300Win=COALESCE((SELECT d1w FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      D1400_1600Top3=COALESCE((SELECT d2 FROM stakes_tmp s WHERE s.pk=horse.pk),0),D1400_1600Win=COALESCE((SELECT d2w FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      D1700_2000Top3=COALESCE((SELECT d3 FROM stakes_tmp s WHERE s.pk=horse.pk),0),D1700_2000Win=COALESCE((SELECT d3w FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      D2100_2400Top3=COALESCE((SELECT d4 FROM stakes_tmp s WHERE s.pk=horse.pk),0),D2100_2400Win=COALESCE((SELECT d4w FROM stakes_tmp s WHERE s.pk=horse.pk),0),
      D2500PTop3=COALESCE((SELECT d5 FROM stakes_tmp s WHERE s.pk=horse.pk),0),D2500PWin=COALESCE((SELECT d5w FROM stakes_tmp s WHERE s.pk=horse.pk),0)""")
    con.execute("UPDATE horse SET Mature5plus=CASE WHEN Age5Top3=1 OR Age6plusTop3=1 THEN 1 ELSE 0 END,TurfDirtBoth=CASE WHEN TurfTop3=1 AND DirtTop3=1 THEN 1 ELSE 0 END,DistanceMulti2=CASE WHEN (D1000_1300Top3+D1400_1600Top3+D1700_2000Top3+D2100_2400Top3+D2500PTop3)>=2 THEN 1 ELSE 0 END,DistanceMulti3=CASE WHEN (D1000_1300Top3+D1400_1600Top3+D1700_2000Top3+D2100_2400Top3+D2500PTop3)>=3 THEN 1 ELSE 0 END")
    if gs is None:
        con.execute("UPDATE horse SET GradeEraEligible=NULL")
    else:
        con.execute("UPDATE horse SET GradeEraEligible=CASE WHEN birth_year IS NULL THEN NULL WHEN birth_year+2>=? THEN 1 ELSE 0 END",(gs,))
    con.execute("""UPDATE horse SET
      G1Reach=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT g1 FROM stakes_tmp s WHERE s.pk=horse.pk),0) END,
      G1Win=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT g1w FROM stakes_tmp s WHERE s.pk=horse.pk),0) END,
      G2Reach=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT g2 FROM stakes_tmp s WHERE s.pk=horse.pk),0) END,
      G2Win=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT g2w FROM stakes_tmp s WHERE s.pk=horse.pk),0) END,
      G3Reach=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT g3 FROM stakes_tmp s WHERE s.pk=horse.pk),0) END,
      G3Win=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT g3w FROM stakes_tmp s WHERE s.pk=horse.pk),0) END,
      ListedReach=CASE WHEN GradeEraEligible=1 THEN COALESCE((SELECT listed FROM stakes_tmp s WHERE s.pk=horse.pk),0) END""")
    con.commit(); return gs


def add_rank_tables(con):
    con.execute("DROP TABLE IF EXISTS ranked_all")
    con.execute("""CREATE TABLE ranked_all AS SELECT h.*,
      PERCENT_RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY Fall) AS FallCountryEraPct,
      PERCENT_RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY F5) AS F5CountryEraPct,
      PERCENT_RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY DeepDelta) AS DeepDeltaCountryEraPct,
      CASE WHEN RecentRatio IS NOT NULL THEN PERCENT_RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY RecentRatio) END AS RecentRatioCountryEraPct,
      CASE WHEN F5SourceHHI IS NOT NULL THEN PERCENT_RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY F5SourceHHI) END AS F5SourceHHICountryEraPct,
      CASE WHEN F5MaxSourceShare IS NOT NULL THEN PERCENT_RANK() OVER(PARTITION BY birth_country,birth5 ORDER BY F5MaxSourceShare) END AS F5MaxSourceShareCountryEraPct
      FROM horse h WHERE AnalysisBirthYearEligible=1""")
    con.execute("CREATE INDEX idx_ranked_all_ce ON ranked_all(birth_country,birth5)")
    con.commit()


def band_of(p):
    if p is None: return ""
    for lo,hi,b in F_BANDS:
        if lo<=p<hi: return b
    return F_BANDS[-1][2]


OUTCOMES=("ScopeFlatStakesTop3","ScopeFlatStakesWin","RepeatWin2","RepeatWin3","RepeatWin5","Top3_5plus","Top3_8plus","MultiYearTop3","Mature5plus","TurfTop3","TurfWin","DirtTop3","DirtWin","TurfDirtBoth","DistanceMulti2","DistanceMulti3","D1000_1300Top3","D1000_1300Win","D1400_1600Top3","D1400_1600Win","D1700_2000Top3","D1700_2000Win","D2100_2400Top3","D2100_2400Win","D2500PTop3","D2500PWin","G1Reach","G1Win","G2Reach","G2Win","G3Reach","G3Win")
METRICS=(("Fall","FallCountryEraPct"),("F5","F5CountryEraPct"),("DeepDelta","DeepDeltaCountryEraPct"),("RecentRatio","RecentRatioCountryEraPct"),("F5SourceHHI","F5SourceHHICountryEraPct"),("F5MaxSourceShare","F5MaxSourceShareCountryEraPct"))


def summarize_bins(con,out,pop_name,where_extra="1=1"):
    fields=["Population","Metric","Band","N","MetricMean","Top3MeanN","WinMeanN","WinConversion"]+[x+"Rate" for x in OUTCOMES]+["GradeEligibleN"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for metric,pct in METRICS:
            q="SELECT "+",".join([metric,pct,"ScopeTop3N","ScopeWinN",*OUTCOMES,"GradeEraEligible"])+f" FROM ranked_all WHERE {where_extra} AND {metric} IS NOT NULL AND {pct} IS NOT NULL"
            groups=defaultdict(list)
            for row in con.execute(q): groups[band_of(row[1])].append(row)
            for _,_,b in F_BANDS:
                g=groups.get(b,[])
                if not g: continue
                rec={"Population":pop_name,"Metric":metric,"Band":b,"N":len(g),"MetricMean":sum(float(x[0]) for x in g)/len(g),"Top3MeanN":sum(x[2] or 0 for x in g)/len(g),"WinMeanN":sum(x[3] or 0 for x in g)/len(g)}
                top=sum(x[2] or 0 for x in g); wins=sum(x[3] or 0 for x in g); rec["WinConversion"]=wins/top if top else ""
                base=4
                for j,o in enumerate(OUTCOMES):
                    vv=[x[base+j] for x in g if x[base+j] is not None]
                    rec[o+"Rate"]=sum(vv)/len(vv) if vv else ""
                rec["GradeEligibleN"]=sum(1 for x in g if x[-1]==1)
                w.writerow(rec)


def summarize_structure(con,out):
    fields=["Population","Axis","Group","N","FallMean","F5Mean","DeepDeltaMean"]+[x+"Rate" for x in OUTCOMES]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for pop,where in (("ALL_BLOOD","1=1"),("STAKES_TOP3","ScopeFlatStakesTop3=1")):
            for axis in ("F5StructureClass","F5NearestCross"):
                q=f"SELECT {axis},COUNT(*),AVG(Fall),AVG(F5),AVG(DeepDelta),"+",".join(f"AVG({o})" for o in OUTCOMES)+f" FROM ranked_all WHERE {where} GROUP BY {axis} ORDER BY COUNT(*) DESC"
                for row in con.execute(q):
                    rec={"Population":pop,"Axis":axis,"Group":row[0] or "NONE","N":row[1],"FallMean":row[2],"F5Mean":row[3],"DeepDeltaMean":row[4]}
                    for i,o in enumerate(OUTCOMES): rec[o+"Rate"]=row[5+i]
                    w.writerow(rec)


def summarize_conditional(con,out):
    fields=["Population","Direction","InnerGroup","N","FallMean","F5Mean","DeepDeltaMean"]+[x+"Rate" for x in OUTCOMES]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for pop,where in (("ALL_BLOOD","1=1"),("STAKES_TOP3","ScopeFlatStakesTop3=1")):
            for name,outerpct,inner in (("Fall固定→F5","FallCountryEraPct","F5"),("F5固定→Fall","F5CountryEraPct","Fall")):
                con.execute("DROP TABLE IF EXISTS _c1"); con.execute("DROP TABLE IF EXISTS _c2")
                con.execute(f"CREATE TEMP TABLE _c1 AS SELECT *,MIN(19,CAST({outerpct}*20 AS INTEGER)) outer20 FROM ranked_all WHERE {where} AND {outerpct} IS NOT NULL AND {inner} IS NOT NULL")
                con.execute(f"CREATE TEMP TABLE _c2 AS SELECT *,PERCENT_RANK() OVER(PARTITION BY birth_country,birth5,outer20 ORDER BY {inner}) innerpct FROM _c1")
                q="SELECT CASE WHEN innerpct<0.333333 THEN 'LOW' WHEN innerpct<0.666667 THEN 'MID' ELSE 'HIGH' END g,COUNT(*),AVG(Fall),AVG(F5),AVG(DeepDelta),"+",".join(f"AVG({o})" for o in OUTCOMES)+" FROM _c2 GROUP BY g"
                for row in con.execute(q):
                    rec={"Population":pop,"Direction":name,"InnerGroup":row[0],"N":row[1],"FallMean":row[2],"F5Mean":row[3],"DeepDeltaMean":row[4]}
                    for i,o in enumerate(OUTCOMES): rec[o+"Rate"]=row[5+i]
                    w.writerow(rec)


def summarize_sire_birth5(con,out):
    fields=["Population","Metric","Group","N","MeanMetric","MeanCellN"]+[x+"Rate" for x in OUTCOMES]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for pop,where in (("ALL_BLOOD","1=1"),("STAKES_TOP3","ScopeFlatStakesTop3=1")):
            for metric in ("Fall","F5","DeepDelta"):
                con.execute("DROP TABLE IF EXISTS _sire")
                con.execute(f"CREATE TEMP TABLE _sire AS SELECT *,COUNT(*) OVER(PARTITION BY sire,birth5) celln,PERCENT_RANK() OVER(PARTITION BY sire,birth5 ORDER BY {metric}) p FROM ranked_all WHERE {where} AND sire<>'' AND birth5 IS NOT NULL")
                q="SELECT CASE WHEN p<0.25 THEN 'LOW25' WHEN p<0.75 THEN 'MID50' ELSE 'HIGH25' END g,COUNT(*),AVG("+metric+"),AVG(celln),"+",".join(f"AVG({o})" for o in OUTCOMES)+" FROM _sire GROUP BY g"
                for row in con.execute(q):
                    rec={"Population":pop,"Metric":metric,"Group":row[0],"N":row[1],"MeanMetric":row[2],"MeanCellN":row[3]}
                    for i,o in enumerate(OUTCOMES): rec[o+"Rate"]=row[4+i]
                    w.writerow(rec)


def inv3(m):
    a,b,c=m[0];d,e,f=m[1];g,h,i=m[2]
    A=e*i-f*h;B=-(d*i-f*g);C=d*h-e*g;D=-(b*i-c*h);E=a*i-c*g;F=-(a*h-b*g);G=b*f-c*e;H=-(a*f-c*d);I=a*e-b*d
    det=a*A+b*B+c*C
    if abs(det)<1e-14:return None
    return [[A/det,D/det,G/det],[B/det,E/det,H/det],[C/det,F/det,I/det]]


def cohort_stats(con,metric,where):
    d={}
    q=f"SELECT birth_country,birth5,COUNT(*),AVG({metric}),AVG({metric}*{metric}) FROM ranked_all WHERE {where} AND {metric} IS NOT NULL GROUP BY birth_country,birth5"
    for c,b,n,m,m2 in con.execute(q):
        var=max(0.0,float(m2)-float(m)*float(m)); d[(c,b)]=(float(m),math.sqrt(var))
    return d


def fit_quad(con,metric,outcome,where):
    stats=cohort_stats(con,metric,where); data=[]
    q=f"SELECT birth_country,birth5,{metric},{outcome} FROM ranked_all WHERE {where} AND {metric} IS NOT NULL AND {outcome} IS NOT NULL"
    for c,b,x,y in con.execute(q):
        st=stats.get((c,b))
        if not st or st[1]<=0: continue
        data.append(((float(x)-st[0])/st[1],int(y)))
    if len(data)<3 or sum(y for _,y in data) in (0,len(data)): return None
    beta=[0.,0.,0.]
    for _ in range(60):
        grad=[0.,0.,0.]; H=[[0.]*3 for _ in range(3)]
        for x,y in data:
            z=(1.,x,x*x); eta=max(-35,min(35,sum(beta[j]*z[j] for j in range(3)))); p=1/(1+math.exp(-eta)); rr=y-p; ww=max(1e-12,p*(1-p))
            for j in range(3):
                grad[j]+=z[j]*rr
                for k in range(3): H[j][k]+=z[j]*z[k]*ww
        iv=inv3(H)
        if iv is None:return None
        step=[sum(iv[j][k]*grad[k] for k in range(3)) for j in range(3)]; beta=[beta[j]+step[j] for j in range(3)]
        if max(abs(s) for s in step)<1e-8: break
    iv=inv3(H); se=[math.sqrt(max(0,iv[j][j])) for j in range(3)] if iv else [None]*3
    vertex=-beta[1]/(2*beta[2]) if abs(beta[2])>1e-12 else None
    return len(data),sum(y for _,y in data),beta,se,vertex


def write_logit(con,out):
    metrics=("Fall","F5","DeepDelta","RecentRatio")
    outcomes=("ScopeFlatStakesTop3","ScopeFlatStakesWin","RepeatWin2","RepeatWin5","Top3_5plus","Mature5plus","MultiYearTop3","G1Reach","G1Win","G2Reach","G3Reach")
    fields=["Population","Metric","Outcome","N","SuccessN","BetaZ","SEZ","PZ","BetaZ2","SEZ2","PZ2","VertexSD","Note"]
    with open(out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for pop,where in (("ALL_BLOOD","1=1"),("STAKES_TOP3","ScopeFlatStakesTop3=1")):
            for m in metrics:
                for o in outcomes:
                    fit=fit_quad(con,m,o,where)
                    if not fit:continue
                    n,s,b,se,v=fit;p1=math.erfc(abs(b[1]/se[1])/math.sqrt(2)) if se[1] else None;p2=math.erfc(abs(b[2]/se[2])/math.sqrt(2)) if se[2] else None
                    w.writerow({"Population":pop,"Metric":m,"Outcome":o,"N":n,"SuccessN":s,"BetaZ":b[1],"SEZ":se[1],"PZ":p1,"BetaZ2":b[2],"SEZ2":se[2],"PZ2":p2,"VertexSD":v,"Note":"Vertexは観察曲線の頂点であり最適近交値ではない"})


def export_query(con,q,path):
    cur=con.execute(q); fields=[d[0] for d in cur.description]
    with open(path,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f);w.writerow(fields)
        for row in cur:w.writerow(["" if x is None else x for x in row])


def write_report(con,path,blood,stakes,scope,gs,ystart,yend):
    total=con.execute("SELECT COUNT(*) FROM horse").fetchone()[0]
    analy=con.execute("SELECT COUNT(*) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0]
    top3=con.execute("SELECT SUM(ScopeFlatStakesTop3) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0] or 0
    grade=con.execute("SELECT SUM(CASE WHEN GradeEraEligible=1 THEN 1 ELSE 0 END) FROM horse WHERE AnalysisBirthYearEligible=1").fetchone()[0] or 0
    txt=f"""近交係数 × 平地stakes 多軸監査\nVersion: {VERSION}\n\n入力\n  blood: {blood}\n  stakes:\n"""+"\n".join("    "+str(x) for x in stakes)+f"""\n\n開催scope: {scope}\n対象馬出生年: {ystart if ystart is not None else 'ALL'} ～ {yend if yend is not None else 'ALL'}\n\nblood全頭N: {total:,}\n解析出生年内N: {analy:,}\nscope内平地stakes Top3経験N: {int(top3):,}\nGrade時代評価可能N: {int(grade):,}\nGrade開始年: {gs if gs is not None else 'scopeが複数国/未定義のため一律分母を作らない'}\n\n重要\n・all_horse_inbreeding_exact*.csvは不要。Fall/F5はblood.csvから内部計算。\n・inbreeding_all_blood_horses.csv はbloodの全馬を出す。\n・stakes未登場馬は StakesRowPresent=0 / ScopeFlatStakesTop3=0。これは「出走して負けた」の意味ではない。\n・Grade制度以前はG1/G2/G3を0にしない。GradeEraEligible=0なら各Grade列はNA。\n・JRAは1984、NARは1997、JPN_ALLは保守的に1997をGrade比較開始年とする。\n・USA/CAN=1973、GB/IRE/FR=1971。未知scopeではGrade分母を自動生成しない。\n・人数による除外は一切しない。小NはNを見て解釈する。\n・Fall/F5/DeepDelta/RecentRatioは能力点ではない。\n・低F側低下をoutbreeding depressionと即断しない。\n・高F側の不利と「Fを下げれば強くなる」は別命題。\n・二次曲線Vertexを育種上の最適Fにしない。\n"""
    Path(path).write_text(txt,encoding="utf-8")


def self_test():
    by={"A":Horse("A"),"B":Horse("B"),"C":Horse("C"),
        "H1":Horse("H1",sire="A",dam="B"),"H2":Horse("H2",sire="A",dam="C"),"X":Horse("X",sire="H1",dam="H2"),
        "F1":Horse("F1",sire="A",dam="B"),"F2":Horse("F2",sire="A",dam="B"),"Y":Horse("Y",sire="F1",dam="F2"),
        "P":Horse("P",sire="A",dam="B"),"Z":Horse("Z",sire="A",dam="P")}
    e=ExactFEngine(by);f5=F5Engine(by,e)
    for pk,expect in (("X",.125),("Y",.25),("Z",.25)):
        assert abs(e.f(pk)-expect)<1e-10,(pk,e.f(pk),expect)
        assert abs(f5.f5(pk)-expect)<1e-10,(pk,f5.f5(pk),expect)
    assert scope_label(["JPN"],"ALL")=="JPN_ALL"
    assert grade_start_for_scope(["JPN"],"JRA")==1984
    assert grade_start_for_scope(["JPN"],"NAR")==1997
    print("SELF TEST OK: half-sib=12.5%, full-sib=25%, parent-offspring=25%, JRA/NAR grade eras OK")


def main():
    print("="*76);print("近交係数 × 競走成績 多軸監査",VERSION);print("="*76)
    if "--self-test" in sys.argv:self_test();return
    blood,stakes=discover_inputs();codes,js=choose_scope();ystart,yend=choose_years();scope=scope_label(codes,js)
    print("\n開催scope:",scope)
    by=load_blood(blood)
    exact=init_exact_cache(CACHE_DB,blood);compute_all_exact(by,exact)
    outdir=SCRIPT_DIR/f"inbreeding_allblood_results_{scope.replace(',','-')}_{ystart or 'ALL'}-{yend or 'ALL'}_{time.strftime('%Y%m%d_%H%M%S')}";outdir.mkdir(parents=True,exist_ok=True)
    work=create_work_db(outdir/"inbreeding_work.sqlite3",by,CACHE_DB)
    parse_stakes_to_db(stakes,work,codes,js);gs=apply_scope_outcomes(work,codes,js,ystart,yend);add_rank_tables(work)
    export_query(work,"SELECT * FROM horse ORDER BY birth_year,pk",outdir/"inbreeding_all_blood_horses.csv")
    export_query(work,"SELECT * FROM ranked_all WHERE ScopeFlatStakesTop3=1 ORDER BY birth_year,pk",outdir/"inbreeding_stakes_top3_horses.csv")
    summarize_bins(work,outdir/"inbreeding_allblood_bin_effects.csv","ALL_BLOOD","1=1")
    summarize_bins(work,outdir/"inbreeding_stakes_bin_effects.csv","STAKES_TOP3","ScopeFlatStakesTop3=1")
    summarize_conditional(work,outdir/"inbreeding_conditional_depth.csv")
    summarize_sire_birth5(work,outdir/"inbreeding_sire_birth5.csv")
    summarize_structure(work,outdir/"inbreeding_structure.csv")
    write_logit(work,outdir/"inbreeding_logistic_quadratic.csv")
    write_report(work,outdir/"inbreeding_report.txt",blood,stakes,scope,gs,ystart,yend)
    meta={"version":VERSION,"blood":str(blood),"stakes":[str(x) for x in stakes],"scope":scope,"birth_start":ystart,"birth_end":yend,"all_blood_output":True,"no_N_cutoff":True,"grade_start":gs,"grade_rule":"pre-grade era -> NA; grade field only; series_grade never used as grade"}
    (outdir/"run_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    exact.close();work.close();print("\n完了:",outdir);print("まず見る: inbreeding_report.txt / inbreeding_allblood_bin_effects.csv / inbreeding_conditional_depth.csv")

if __name__=="__main__":main()
