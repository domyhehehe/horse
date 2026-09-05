#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbreeding_performance_scope_allblood_pyto_v1_0.py
==================================================
Pyto向け「近交係数 × 平地stakes成績」多軸監査。

設計上の重要点
--------------
* 必須入力は blood.csv と stakes_horses*.csv だけ。
  all_horse_inbreeding_exact*.csv のような事前計算済みFファイルは要求しない。
* blood.csv から全世代Exact F (Fall) と厳密5代F (F5) を内部計算する。
* blood.csv の全馬を horse-level CSV に出す。stakes DBにいない馬も落とさない。
* StakesDBPresent と ScopeFlatStakesTop3 を分離する。
  stakesにいない馬を「出走して負けた馬」とは解釈しない。
* 開催国scopeはIK/GBS系と同じ思想。生産国は母集団フィルタにしない。
* 日本は JPN_ALL / JRA / NAR を切替可能。
* 人数による隠れた除外は一切しない。N=1セルも保存する。
* Gradeはstakes CSVの grade 列だけを使い、series_gradeを代用しない。
* グレード制度成立前に後付けされたGrade I等をそのまま使わない。
  GradeEraEligibleで制度上の観測可能性を分離し、制度前は0ではなくNA。
* 低F側の低成績をoutbreeding depressionと自動解釈しない。
* F5SourceHHI/MaxSourceShareは5代内simple-loop寄与の構造proxyであり、
  Exact Fの厳密な祖先別partial-F分解ではない。

出力
----
  inbreeding_all_blood_horses.csv       blood全頭
  inbreeding_scope_stakes_horses.csv    選択scope平地stakes Top3馬
  inbreeding_era_bins.csv               F帯×成績（BLOOD_ALL / STAKES_TOP3）
  inbreeding_conditional_depth.csv      Fall固定→F5 / F5固定→Fall
  inbreeding_sire_birth5.csv             同父×出生5年帯
  inbreeding_structure.csv               5代近交構造
  inbreeding_logistic_quadratic.csv      z + z^2 ロジスティック
  inbreeding_report.txt
  run_metadata.json

注意
----
BLOOD_ALLの「成功率」は公式な出走馬成功率ではない。
「blood DB収録馬のうち、選択scopeのstakes DBに成功記録が現れる割合」である。
特にJRA/NAR等の開催scopeでは、stakes未収録馬がその市場で実際に出走したかは不明。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

SCRIPT_VERSION = "1.0.0-zero-based-allblood"
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_NAME = ".inbreeding_exact_f5_cache_v1.sqlite3"

DISTANCE_BANDS = (
    ("1000_1300", 1000, 1300, "1000-1300"),
    ("1400_1600", 1400, 1600, "1400-1600"),
    ("1700_2000", 1700, 2000, "1700-2000"),
    ("2100_2400", 2100, 2400, "2100-2400"),
    ("2500_plus", 2500, None, "2500+"),
)
DISTANCE_KEYS = tuple(x[0] for x in DISTANCE_BANDS)

F_BANDS = (
    (0.00, 0.01, "00-01%"),
    (0.01, 0.05, "01-05%"),
    (0.05, 0.10, "05-10%"),
    (0.10, 0.25, "10-25%"),
    (0.25, 0.75, "25-75%"),
    (0.75, 0.90, "75-90%"),
    (0.90, 0.95, "90-95%"),
    (0.95, 0.99, "95-99%"),
    (0.99, 1.0000001, "99-100%"),
)

OUTPUT_COUNTRY_QUICK_MENU = (
    ("JPN", "日本"), ("USA", "米国"), ("GB", "英国"), ("IRE", "アイルランド"),
    ("FR", "フランス"), ("AUS", "豪州"), ("CAN", "カナダ"), ("GER", "ドイツ"),
    ("NZ", "ニュージーランド"), ("UAE", "UAE"), ("HK", "香港"),
    ("SAF", "南アフリカ"), ("ARG", "アルゼンチン"), ("BRZ", "ブラジル"),
    ("TURKIYE", "トルコ"), ("CHI", "チリ"), ("INDIA", "インド"), ("KOR", "韓国"),
)

COUNTRY_ALIASES = {
    "JAPAN":"JPN", "JPN":"JPN", "日本":"JPN",
    "USA":"USA", "US":"USA", "UNITED STATES":"USA", "UNITED STATES OF AMERICA":"USA",
    "米国":"USA", "アメリカ":"USA",
    "GREAT BRITAIN":"GB", "GB":"GB", "GBR":"GB", "ENGLAND":"GB", "UK":"GB",
    "英国":"GB", "イギリス":"GB",
    "IRELAND":"IRE", "IRE":"IRE", "アイルランド":"IRE",
    "FRANCE":"FR", "FR":"FR", "FRA":"FR", "フランス":"FR",
    "AUSTRALIA":"AUS", "AUS":"AUS", "豪州":"AUS", "オーストラリア":"AUS",
    "CANADA":"CAN", "CAN":"CAN", "カナダ":"CAN",
    "GERMANY":"GER", "GER":"GER", "DE":"GER", "ドイツ":"GER",
    "ITALY":"ITY", "ITY":"ITY", "ITA":"ITY",
    "NEW ZEALAND":"NZ", "NZ":"NZ", "ニュージーランド":"NZ",
    "UNITED ARAB EMIRATES":"UAE", "UAE":"UAE",
    "HONG KONG":"HK", "HK":"HK", "HKG":"HK", "香港":"HK",
    "SOUTH AFRICA":"SAF", "SAF":"SAF", "南アフリカ":"SAF",
    "ARGENTINA":"ARG", "ARG":"ARG", "アルゼンチン":"ARG",
    "BRAZIL":"BRZ", "BRZ":"BRZ", "ブラジル":"BRZ",
    "CHILE":"CHI", "CHI":"CHI", "CHL":"CHI", "チリ":"CHI",
    "TURKEY":"TURKIYE", "TURKIYE":"TURKIYE", "TÜRKIYE":"TURKIYE", "トルコ":"TURKIYE",
    "INDIA":"INDIA", "IND":"INDIA", "インド":"INDIA",
    "KOREA":"KOR", "SOUTH KOREA":"KOR", "KOR":"KOR", "韓国":"KOR",
}

JRA_TRACK_ALIASES = {
    "SAPPORO", "HAKODATE", "FUKUSHIMA", "FUKUCHIMA", "NIIGATA",
    "TOKYO", "TOKYO FUCHU", "TOKYO RACECOURSE", "FUCHU", "FUCHUU",
    "NAKAYAMA", "NAKAYAMA RACECOURSE", "CHUKYO", "KYOTO", "HANSHIN",
    "HANSHIN CHUKYO", "KOKURA",
    "TOKYO MEGURO", "MEGURO TOKYO", "MEGURO", "NARUO",
    "札幌", "函館", "福島", "新潟", "東京", "府中", "中山", "中京", "京都", "阪神", "小倉", "目黒", "鳴尾",
}

# race-host単位のGrade制度開始年。確認できた制度のみ明示。
# USA: American Graded Stakes Committee project 1973
# GB/IRE/FR: European Pattern 1971
# GER: Pattern System 1972
# NZ: Group designation adopted 1984-85 season -> conservative 1984
# Japan is venue-specific: JRA 1984 / dirt graded (NAR含む) 1997.
GRADE_START_BY_COUNTRY = {
    "USA": 1973,
    "GB": 1971,
    "IRE": 1971,
    "FR": 1971,
    "GER": 1972,
    "NZ": 1984,
}

JUMP_PATTERN = re.compile(
    r"(?:\bHURDLES?\b|\bJUMPS?\b|\bSTEEPLE(?:CHASE)?S?\b|\bNOVICES?\s+CHASE\b|"
    r"\bHANDICAP\s+CHASE\b|\bCHAMPION\s+CHASE\b|\bCHASE\b|\bNATIONAL\s+HUNT\b|"
    r"\bNH\s+FLAT\b|\bPOINT\s+TO\s+POINT\b|\bCROSS\s+COUNTRY\b|障害|ハードル|スティープルチェイス)",
    re.IGNORECASE,
)
KNOWN_JUMP_PATTERN = re.compile(
    r"\b(COLONIAL CUP|THEODORA A\.? RANDOLPH CUP|FOXHUNTERS BOWL|JAMES STUMP MEMORIAL|"
    r"PENNSYLVANIA HUNT CUP|VIRGINIA HUNT CUP|VIRGINIA GOLD CUP|MIDDLEBURG HUNT CUP|"
    r"NEW JERSEY HUNT CUP)\b", re.IGNORECASE,
)


@dataclass(frozen=True)
class PedigreeRec:
    sire: str
    dam: str
    year: Optional[int]
    country: str
    name: str
    sex: str


@dataclass
class OutcomeRec:
    stakes_db_present: int = 0
    scope_top3_n: int = 0
    scope_win_n: int = 0
    years: Set[int] = field(default_factory=set)
    ages: Set[int] = field(default_factory=set)
    turf_top3_n: int = 0
    turf_win_n: int = 0
    dirt_top3_n: int = 0
    dirt_win_n: int = 0
    synth_top3_n: int = 0
    synth_win_n: int = 0
    dist_top3: Dict[str, int] = field(default_factory=lambda: {k:0 for k in DISTANCE_KEYS})
    dist_win: Dict[str, int] = field(default_factory=lambda: {k:0 for k in DISTANCE_KEYS})
    grade_era_eligible: int = 0
    g1_reach: int = 0
    g1_win: int = 0
    g2_reach: int = 0
    g2_win: int = 0
    g3_reach: int = 0
    g3_win: int = 0
    listed_reach: int = 0


def normalize_token(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return re.sub(r"[\s\u3000]+", " ", text)


def normalize_country(value) -> str:
    text = normalize_token(value)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^0-9A-Z\u3040-\u30FF\u3400-\u9FFF]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return COUNTRY_ALIASES.get(text, text)


def normalize_track(value) -> str:
    text = normalize_token(value)
    text = re.sub(r"[^0-9A-Z\u3040-\u30FF\u3400-\u9FFF]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_japan_scope(value) -> str:
    text = normalize_token(value or "ALL")
    aliases = {
        "ALL":"ALL", "JPN ALL":"ALL", "JPN_ALL":"ALL", "TOTAL":"ALL", "総合":"ALL", "全体":"ALL", "0":"ALL", "1":"ALL",
        "JRA":"JRA", "CENTRAL":"JRA", "中央":"JRA", "2":"JRA",
        "NAR":"NAR", "LOCAL":"NAR", "地方":"NAR", "3":"NAR",
    }
    return aliases.get(text, text)


def classify_japan_venue(race: dict) -> str:
    if normalize_country((race or {}).get("country")) != "JPN":
        return ""
    track = normalize_track((race or {}).get("track"))
    if not track:
        return "UNKNOWN"
    return "JRA" if track in JRA_TRACK_ALIASES else "NAR"


def analysis_scope_label(codes: Optional[Sequence[str]], japan_scope: str) -> str:
    if codes == ["JPN"]:
        js = normalize_japan_scope(japan_scope)
        return "JPN_ALL" if js == "ALL" else js
    return "ALL" if not codes else ",".join(codes)


def race_country_allowed(race: dict, codes: Optional[Sequence[str]], japan_scope: str) -> bool:
    if codes and normalize_country(race.get("country")) not in set(codes):
        return False
    js = normalize_japan_scope(japan_scope)
    if js == "ALL":
        return True
    if list(codes or []) != ["JPN"]:
        return False
    return classify_japan_venue(race) == js


def parse_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def normalize_surface(value) -> str:
    text = normalize_token(value)
    if any(x in text for x in ("SYNTHETIC","SYNTH","ALL WEATHER","POLYTRACK","TAPETA","AWT")):
        return "SYNTHETIC"
    if any(x in text for x in ("DIRT","SAND","ダート")):
        return "DIRT"
    if any(x in text for x in ("TURF","GRASS","芝")):
        return "TURF"
    return ""


def normalize_grade_strict(value) -> str:
    text = normalize_token(value)
    if not text:
        return ""
    compact = re.sub(r"[._\-\s]+", "", text)
    if compact in {"G1","GRADE1","GROUP1","GI","GRADEI","GROUPI","JPN1","JPNI"}:
        return "G1"
    if compact in {"G2","GRADE2","GROUP2","GII","GRADEII","GROUPII","JPN2","JPNII"}:
        return "G2"
    if compact in {"G3","GRADE3","GROUP3","GIII","GRADEIII","GROUPIII","JPN3","JPNIII"}:
        return "G3"
    if "LISTED" in text or compact in {"L","LR"}:
        return "LISTED"
    return ""


def parse_distance_m(value) -> Optional[int]:
    raw = normalize_token(value).replace(",", "")
    if not raw:
        return None
    raw = raw.replace("¼",".25").replace("½",".5").replace("¾",".75")
    text = re.sub(r"\([^)]*\)", "", raw)
    text = re.sub(r"\b(?:ABOUT|APPROXIMATELY|APPROX|ABT)\b", "", text)
    text = re.sub(r"\s+", "", text)
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:KM|KILOMET(?:ER|RE)S?)", text)
    if m:
        return int(round(float(m.group(1))*1000))
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:MET(?:ER|RE)S?|MTS?|MTRS?)", text)
    if m:
        return int(round(float(m.group(1))))
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)M", text)
    if m:
        x = float(m.group(1))
        return int(round(x if x >= 100 else x*1609.344))
    miles = furlongs = yards = 0.0
    found = False
    for z in re.finditer(r"([0-9]+(?:\.[0-9]+)?)(MILES?|MILE|MI|M)(?![A-Z])", text):
        miles += float(z.group(1)); found = True
    for z in re.finditer(r"([0-9]+(?:\.[0-9]+)?)(?:FURLONGS?|F)(?![A-Z])", text):
        furlongs += float(z.group(1)); found = True
    for z in re.finditer(r"([0-9]+(?:\.[0-9]+)?)(?:YARDS?|Y)(?![A-Z])", text):
        yards += float(z.group(1)); found = True
    if found:
        return int(round(miles*1609.344 + furlongs*201.168 + yards*0.9144))
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)", text)
    if m and float(m.group(1)) >= 100:
        return int(round(float(m.group(1))))
    return None


def distance_band_key(distance: Optional[int]) -> Optional[str]:
    if distance is None:
        return None
    for key, lo, hi, _ in DISTANCE_BANDS:
        if distance >= lo and (hi is None or distance <= hi):
            return key
    return None


def is_jump_race(race: dict) -> bool:
    race_name = str(race.get("race_name") or "")
    distance = str(race.get("distance") or "")
    comment = str(race.get("comment") or "")
    combined = f"{race_name} {distance} {comment}"
    if JUMP_PATTERN.search(combined) or KNOWN_JUMP_PATTERN.search(race_name):
        return True
    if re.search(r"\bGRAND\s+(ANNUAL|NATIONAL)\b", race_name, re.I):
        return True
    if re.search(r"\bOVER\s+(?:WOOD|FENCES?)\b|\(H\)", distance, re.I):
        return True
    return False


def grade_era_valid(race: dict) -> Optional[bool]:
    year = parse_int(race.get("year"))
    if year is None:
        return None
    country = normalize_country(race.get("country"))
    if country == "JPN":
        venue = classify_japan_venue(race)
        if venue == "JRA":
            return year >= 1984
        if venue == "NAR":
            return year >= 1997
        # track不明では1984-96に中央/地方のどちらか判別不能。
        # 1997以降だけ保守的にGrade-eraとして扱う。
        return year >= 1997
    start = GRADE_START_BY_COUNTRY.get(country)
    if start is None:
        return None
    return year >= start


def choose_race_countries_pyto() -> Optional[List[str]]:
    print("\n対象レース開催国")
    print("  0 : ALL / 全世界")
    for i, (code, label) in enumerate(OUTPUT_COUNTRY_QUICK_MENU, 1):
        print(f" {i:>2} : {label} ({code})")
    print(" 99 : 国コード/国名を直接入力（複数はカンマ区切り）")
    lookup = {str(i): code for i,(code,_label) in enumerate(OUTPUT_COUNTRY_QUICK_MENU,1)}
    while True:
        raw = input("開催国 [空欄/0=ALL、1=日本、2=米国]: ").strip()
        if not raw or raw == "0":
            return None
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        if parts == ["99"]:
            custom = input("国コード/国名 [例 JPN,USA]: ").strip()
            vals = []
            for x in custom.split(","):
                c = normalize_country(x)
                if c and c not in vals:
                    vals.append(c)
            if vals:
                return vals
            continue
        vals=[]; bad=[]
        for p in parts:
            if p in lookup:
                vals.append(lookup[p])
            else:
                bad.append(p)
        if bad:
            print("未対応番号:", ",".join(bad)); continue
        vals = list(dict.fromkeys(vals))
        if vals:
            return vals


def choose_japan_scope_pyto() -> str:
    print("\n日本開催の範囲")
    print("  1 : 総合（JRA＋NAR＋track不明）")
    print("  2 : 中央（JRA）")
    print("  3 : 地方（NAR）")
    while True:
        raw = input("日本開催範囲 [空欄/1=総合、2=中央、3=地方]: ").strip()
        scope = normalize_japan_scope(raw or "ALL")
        if scope in {"ALL","JRA","NAR"}:
            return scope


def normalize_dash(text: str) -> str:
    return (str(text or "").replace("－","-").replace("–","-").replace("—","-")
            .replace("―","-").replace("〜","-").replace("～","-").replace("~","-"))


def parse_year_range_input(text: str) -> Tuple[Optional[int],Optional[int]]:
    token = normalize_dash(text).replace(" ","").strip()
    if not token:
        return None,None
    if re.fullmatch(r"\d{4}", token):
        y=int(token); return y,y
    if re.fullmatch(r"\d{4}-\d{4}", token):
        a,b=map(int,token.split("-",1)); return min(a,b),max(a,b)
    if re.fullmatch(r"\d{4}-", token):
        return int(token[:-1]),None
    if re.fullmatch(r"-\d{4}", token):
        return None,int(token[1:])
    raise ValueError("年代は 1990-2020、1990、1990-、-2020 の形で入力してください。")


def discover_inputs(base_dir: Path) -> Tuple[Path,List[Path]]:
    exact_blood = base_dir / "blood.csv"
    if exact_blood.is_file():
        blood = exact_blood
    else:
        cands = [p for p in base_dir.glob("blood*.csv") if p.is_file() and "result" not in p.name.lower()]
        if not cands:
            raise FileNotFoundError("blood.csv / blood*.csv がありません。")
        blood = max(cands, key=lambda p:(p.stat().st_size,p.stat().st_mtime_ns))

    exact_stakes = base_dir / "stakes_horses.csv"
    if exact_stakes.is_file():
        stakes=[exact_stakes]
    else:
        cands=[p for p in base_dir.glob("stakes_horses*.csv") if p.is_file() and "result" not in p.name.lower()]
        if not cands:
            raise FileNotFoundError("stakes_horses.csv / stakes_horses*.csv がありません。")
        # 完全版が1本だけならそれを使う。複数なら歴史的3分割を全てstreamする。
        nonsplit=[p for p in cands if not any(t in unicodedata.normalize("NFKC",p.stem).lower()
                  for t in ("前半_前半","前半_後半","コピー_後半","part1","part2","part3","split1","split2","split3"))]
        if len(nonsplit)==1:
            stakes=nonsplit
        else:
            stakes=sorted(cands)
    return blood,stakes


def quick_file_signature(path: Path) -> str:
    st=path.stat()
    raw=f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_pedigree(path: Path) -> Dict[str,PedigreeRec]:
    pedigree={}
    with path.open("r",encoding="utf-8-sig",newline="") as f:
        r=csv.DictReader(f)
        required={"PrimaryKey","Sire","Dam","Year","Country","Horse Name"}
        missing=required-set(r.fieldnames or [])
        if missing:
            raise ValueError(f"blood CSV missing columns: {sorted(missing)}")
        sex_col="Sex" if "Sex" in (r.fieldnames or []) else None
        for row in r:
            pk=(row.get("PrimaryKey") or "").strip()
            if not pk:
                continue
            pedigree[pk]=PedigreeRec(
                sire=(row.get("Sire") or "").strip(), dam=(row.get("Dam") or "").strip(),
                year=parse_int(row.get("Year")), country=normalize_country(row.get("Country")),
                name=(row.get("Horse Name") or pk).strip(), sex=(row.get(sex_col) or "").strip() if sex_col else "",
            )
    return pedigree


def topological_order(pedigree: Dict[str,PedigreeRec]) -> List[str]:
    sys.setrecursionlimit(max(1000000, len(pedigree)+1000))
    state={}
    order=[]
    def visit(pk: str):
        st=state.get(pk,0)
        if st==2:
            return
        if st==1:
            raise ValueError(f"pedigree cycle detected near {pk}")
        state[pk]=1
        h=pedigree[pk]
        for p in (h.sire,h.dam):
            if p and p!=pk and p in pedigree:
                visit(p)
        state[pk]=2
        order.append(pk)
    for n,pk in enumerate(pedigree,1):
        if state.get(pk,0)!=2:
            visit(pk)
        if n%100000==0:
            print(f"  topo {n:,}/{len(pedigree):,}")
    return order


def compute_all_generation_exact(pedigree: Dict[str,PedigreeRec], order: Sequence[str]):
    """Sparse Henderson/LDL' diagonal recursion.

    A = T D T' の対角 A_ii を、個体iから祖先への疎なT係数だけ展開して求める。
    F_i = A_ii - 1。未知親は無関係founderとして扱う。
    """
    n=len(order)
    idx={pk:i for i,pk in enumerate(order)}
    sire=[-1]*n; dam=[-1]*n
    for i,pk in enumerate(order):
        h=pedigree[pk]
        sire[i]=idx.get(h.sire,-1) if h.sire!=pk else -1
        dam[i]=idx.get(h.dam,-1) if h.dam!=pk else -1
    F=[0.0]*n; D=[1.0]*n; trace=[1]*n
    pair_f={}
    for i,pk in enumerate(order):
        s,d=sire[i],dam[i]
        if s>=0 and d>=0:
            D[i]=0.5-0.25*(F[s]+F[d])
        elif s>=0:
            D[i]=0.75-0.25*F[s]
        elif d>=0:
            D[i]=0.75-0.25*F[d]
        else:
            D[i]=1.0

        if s<0 or d<0:
            F[i]=0.0
            trace[i]=1+(1 if s>=0 else 0)+(1 if d>=0 else 0)
        elif s==d:
            F[i]=0.5*(1.0+F[s])
            trace[i]=2
        else:
            key=(s,d) if s<d else (d,s)
            reused=pair_f.get(key)
            if reused is not None:
                F[i]=reused
                trace[i]=0
            else:
                coef={i:1.0}
                heap=[-i]
                queued={i}
                diag=0.0; nodes=0
                while heap:
                    k=-heapq.heappop(heap)
                    c=coef[k]
                    diag += c*c*D[k]
                    nodes += 1
                    for p in (sire[k],dam[k]):
                        if p>=0:
                            coef[p]=coef.get(p,0.0)+0.5*c
                            if p not in queued:
                                queued.add(p)
                                heapq.heappush(heap,-p)
                fi=diag-1.0
                if abs(fi)<1e-14:
                    fi=0.0
                F[i]=fi
                trace[i]=nodes
                pair_f[key]=fi
        if (i+1)%10000==0:
            print(f"  Exact F {i+1:,}/{n:,}")
    return idx,F,D,trace,sire,dam


def five_gen_nodes_and_coverage(pk: str, pedigree: Dict[str,PedigreeRec]):
    known_slots=0; max_depth=0; min_depth={}
    frontier=[pk]
    for gen in range(1,6):
        nxt=[]
        for child in frontier:
            h=pedigree.get(child)
            if not h:
                continue
            for p in (h.sire,h.dam):
                if p and p in pedigree and p!=child:
                    known_slots += 1
                    max_depth=max(max_depth,gen)
                    if p not in min_depth or gen<min_depth[p]:
                        min_depth[p]=gen
                    nxt.append(p)
        frontier=nxt
        if not frontier:
            break
    return min_depth,known_slots,max_depth


def compute_local_exact_f5(pk: str, pedigree: Dict[str,PedigreeRec], global_idx: Dict[str,int]):
    h0=pedigree.get(pk)
    if not h0:
        return 0.0,0,0.0,0
    min_depth,known_slots,max_depth=five_gen_nodes_and_coverage(pk,pedigree)
    coverage=known_slots/62.0
    nodes=set(min_depth); nodes.add(pk)
    local_order=sorted(nodes,key=lambda x:global_idx[x])
    li={x:i for i,x in enumerate(local_order)}
    n=len(local_order); F=[0.0]*n; D=[1.0]*n
    sarr=[-1]*n; darr=[-1]*n
    for i,x in enumerate(local_order):
        h=pedigree[x]
        # targetの親は許可。祖先は、その祖先の最短出現世代が5未満のときだけ親リンクを許可。
        allow=(x==pk) or (min_depth.get(x,99)<5)
        if allow:
            sarr[i]=li.get(h.sire,-1) if h.sire!=x else -1
            darr[i]=li.get(h.dam,-1) if h.dam!=x else -1
    for i in range(n):
        s,d=sarr[i],darr[i]
        if s>=0 and d>=0: D[i]=0.5-0.25*(F[s]+F[d])
        elif s>=0: D[i]=0.75-0.25*F[s]
        elif d>=0: D[i]=0.75-0.25*F[d]
        else: D[i]=1.0
        if s<0 or d<0:
            F[i]=0.0; continue
        coef={i:1.0}; heap=[-i]; queued={i}; diag=0.0
        while heap:
            k=-heapq.heappop(heap); c=coef[k]; diag+=c*c*D[k]
            for p in (sarr[k],darr[k]):
                if p>=0:
                    coef[p]=coef.get(p,0.0)+0.5*c
                    if p not in queued:
                        queued.add(p); heapq.heappush(heap,-p)
        F[i]=diag-1.0
        if abs(F[i])<1e-14: F[i]=0.0
    return F[li[pk]],known_slots,coverage,max_depth


def paths_from(pedigree: Dict[str,PedigreeRec], start: str, max_edges: int=4):
    out=defaultdict(list)
    if not start or start not in pedigree:
        return out
    stack=[(start,(start,),0)]
    while stack:
        node,path,d=stack.pop()
        if node not in pedigree:
            continue
        out[node].append(path)
        if d>=max_edges:
            continue
        h=pedigree[node]
        for p in (h.sire,h.dam):
            if p and p in pedigree and p not in path:
                stack.append((p,path+(p,),d+1))
    return out


def recent_structure(pk: str, pedigree: Dict[str,PedigreeRec]):
    h=pedigree[pk]
    ps=paths_from(pedigree,h.sire,4); pd=paths_from(pedigree,h.dam,4)
    contrib=defaultdict(float); pair_n=0; nearest=None; nearlabel=""
    for anc in set(ps).intersection(pd):
        for a in ps[anc]:
            for b in pd[anc]:
                if set(a[:-1]).intersection(b[:-1]):
                    continue
                ds=len(a)-1; dd=len(b)-1
                c=0.5**(ds+dd+1)
                contrib[anc]+=c; pair_n+=1
                g1,g2=ds+1,dd+1; lo,hi=sorted((g1,g2)); key=(lo+hi,hi,lo)
                if nearest is None or key<nearest:
                    nearest=key; nearlabel=f"{lo}x{hi}"
    total=sum(contrib.values())
    if total<=0:
        return 0,pair_n,nearlabel,None,None,"NO_RECENT_COMMON"
    shares={a:v/total for a,v in contrib.items()}
    hhi=sum(v*v for v in shares.values()); mx=max(shares.values())
    if len(shares)==1: cls="SINGLE_SOURCE"
    elif mx>=0.60: cls="DOMINANT_SOURCE"
    else: cls="DISTRIBUTED_MULTI_SOURCE"
    return len(shares),pair_n,nearlabel,hhi,mx,cls


def init_cache(path: Path, blood: Path):
    con=sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS metric(
        pk TEXT PRIMARY KEY, fall REAL, f5 REAL, trace_nodes INTEGER,
        known_slots5 INTEGER, coverage5 REAL, max_known_depth INTEGER,
        common_n INTEGER, path_pair_n INTEGER, nearest_cross TEXT,
        source_hhi REAL, max_source_share REAL, structure_class TEXT)""")
    sig=quick_file_signature(blood)
    old=con.execute("SELECT v FROM meta WHERE k='blood_sig'").fetchone()
    if not old or old[0]!=sig:
        con.execute("DELETE FROM metric"); con.execute("DELETE FROM meta")
        con.execute("INSERT INTO meta(k,v) VALUES('blood_sig',?)",(sig,)); con.commit()
    return con


def load_or_compute_metrics(pedigree: Dict[str,PedigreeRec], blood: Path, cache_path: Path):
    con=init_cache(cache_path,blood)
    cached_n=con.execute("SELECT COUNT(*) FROM metric").fetchone()[0]
    if cached_n==len(pedigree):
        print(f"Exact F/F5 cache再利用: {cached_n:,}頭")
        out={}
        for row in con.execute("SELECT pk,fall,f5,trace_nodes,known_slots5,coverage5,max_known_depth,common_n,path_pair_n,nearest_cross,source_hhi,max_source_share,structure_class FROM metric"):
            out[row[0]]=row[1:]
        con.close(); return out

    print("Exact F/F5 cache未完成のため、blood全頭を再計算します。")
    con.execute("DELETE FROM metric"); con.commit()
    order=topological_order(pedigree)
    gidx,F,_D,trace,_s,_d=compute_all_generation_exact(pedigree,order)
    rows=[]; out={}; t0=time.time()
    for i,pk in enumerate(order):
        f5,known,cov,mdepth=compute_local_exact_f5(pk,pedigree,gidx)
        cn,pn,nc,hhi,mx,cls=recent_structure(pk,pedigree)
        vals=(F[i]*100.0,f5*100.0,trace[i],known,cov,mdepth,cn,pn,nc,hhi,mx,cls)
        out[pk]=vals
        rows.append((pk,)+vals)
        if len(rows)>=2000:
            con.executemany("INSERT OR REPLACE INTO metric VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",rows); con.commit(); rows=[]
        if (i+1)%10000==0:
            print(f"  F5/structure {i+1:,}/{len(order):,}  {time.time()-t0:.1f}s")
    if rows:
        con.executemany("INSERT OR REPLACE INTO metric VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",rows); con.commit()
    con.close(); return out


def safe_json_races(raw: str) -> List[dict]:
    try:
        obj=json.loads(raw)
        return obj if isinstance(obj,list) else []
    except Exception:
        return []


def read_stakes(paths: Sequence[Path], pedigree: Dict[str,PedigreeRec], codes, japan_scope, eval_end):
    outcomes={}; stakes_present=set(); total_rows=0; selected_events=0
    for path in paths:
        print("stakes scan:",path.name)
        with path.open("r",encoding="utf-8-sig",newline="") as f:
            r=csv.DictReader(f)
            required={"PrimaryKey","RaceDataJSON"}
            missing=required-set(r.fieldnames or [])
            if missing:
                raise ValueError(f"{path.name} missing columns: {sorted(missing)}")
            for row in r:
                pk=(row.get("PrimaryKey") or "").strip()
                if not pk:
                    continue
                stakes_present.add(pk); total_rows+=1
                o=outcomes.setdefault(pk,OutcomeRec(stakes_db_present=1))
                h=pedigree.get(pk)
                for race in safe_json_races(row.get("RaceDataJSON") or "[]"):
                    if not isinstance(race,dict) or is_jump_race(race):
                        continue
                    if not race_country_allowed(race,codes,japan_scope):
                        continue
                    placing=parse_int(race.get("placing")); year=parse_int(race.get("year"))
                    if placing not in (1,2,3):
                        continue
                    if eval_end is not None and (year is None or year>eval_end):
                        continue
                    selected_events+=1; o.scope_top3_n+=1
                    win=placing==1
                    if win: o.scope_win_n+=1
                    if year is not None:
                        o.years.add(year)
                        if h and h.year is not None:
                            age=year-h.year
                            if 1<=age<=20: o.ages.add(age)
                    surface=normalize_surface(race.get("surface"))
                    if surface=="TURF":
                        o.turf_top3_n+=1; o.turf_win_n+=int(win)
                    elif surface=="DIRT":
                        o.dirt_top3_n+=1; o.dirt_win_n+=int(win)
                    elif surface=="SYNTHETIC":
                        o.synth_top3_n+=1; o.synth_win_n+=int(win)
                    dk=distance_band_key(parse_distance_m(race.get("distance")))
                    if dk:
                        o.dist_top3[dk]+=1; o.dist_win[dk]+=int(win)
                    valid=grade_era_valid(race)
                    if valid is True:
                        o.grade_era_eligible=1
                        gr=normalize_grade_strict(race.get("grade"))
                        if gr=="G1":
                            o.g1_reach=1; o.g1_win=max(o.g1_win,int(win))
                        elif gr=="G2":
                            o.g2_reach=1; o.g2_win=max(o.g2_win,int(win))
                        elif gr=="G3":
                            o.g3_reach=1; o.g3_win=max(o.g3_win,int(win))
                        elif gr=="LISTED":
                            o.listed_reach=1
        print(f"  stakes rows={total_rows:,} selected flat Top3 events={selected_events:,}")
    return outcomes,stakes_present


def average_tie_pct_z(group: List[Tuple[str,float]]):
    arr=sorted(group,key=lambda x:x[1]); n=len(arr); out={}; k=0
    vals=[x[1] for x in arr]; mean=sum(vals)/n
    sd=math.sqrt(sum((v-mean)**2 for v in vals)/(n-1)) if n>1 else 0.0
    while k<n:
        j=k+1
        while j<n and arr[j][1]==arr[k][1]: j+=1
        avg_rank=((k+1)+j)/2.0; pct=(avg_rank-0.5)/n
        for z in range(k,j):
            pk,v=arr[z]; out[pk]=(pct,(v-mean)/sd if sd>0 else 0.0)
        k=j
    return out


def build_era_positions(pedigree,metrics):
    metric_idx={"Fall":0,"F5":1}
    raw={}
    for pk,m in metrics.items():
        fall=m[0]; f5=m[1]
        raw[pk]={"Fall":fall,"F5":f5,"DeepDelta":fall-f5,"RecentRatio":f5/fall if fall>0 else None}
    positions={pk:{} for pk in pedigree}
    for metric in ("Fall","F5","DeepDelta","RecentRatio"):
        global_groups=defaultdict(list); country_groups=defaultdict(list)
        for pk,h in pedigree.items():
            v=raw[pk][metric]
            if v is None or h.year is None: continue
            b5=(h.year//5)*5
            global_groups[b5].append((pk,v))
            if h.country: country_groups[(h.country,b5)].append((pk,v))
        for key,g in global_groups.items():
            for pk,(pct,z) in average_tie_pct_z(g).items():
                positions[pk][metric+"GlobalEraPct"]=pct; positions[pk][metric+"GlobalEraZ"]=z
        for key,g in country_groups.items():
            for pk,(pct,z) in average_tie_pct_z(g).items():
                positions[pk][metric+"CountryEraPct"]=pct; positions[pk][metric+"CountryEraZ"]=z
    return raw,positions


def in_period(year,start,end):
    if start is None and end is None: return True
    if year is None: return False
    if start is not None and year<start: return False
    if end is not None and year>end: return False
    return True


def horse_record(pk,pedigree,metrics,raw,pos,outcomes,stakes_present,start,end):
    h=pedigree[pk]; m=metrics[pk]; o=outcomes.get(pk)
    fall,f5,trace,known,cov,mdepth,cn,pn,nc,hhi,mx,cls=m
    top3=o.scope_top3_n if o else 0; wins=o.scope_win_n if o else 0
    ages=o.ages if o else set(); dist_n=sum(1 for k in DISTANCE_KEYS if o and o.dist_top3[k]>0)
    grade_eligible=(o.grade_era_eligible if o and top3>0 else None)
    rec={
        "PrimaryKey":pk,"HorseName":h.name,"BirthYear":h.year,"BirthCountry":h.country,"Sex":h.sex,
        "SirePK":h.sire,"DamPK":h.dam,"Birth5":(h.year//5)*5 if h.year is not None else None,
        "AnalysisPeriodIncluded":int(in_period(h.year,start,end)),
        "Fall":fall,"F5":f5,"DeepDelta":fall-f5,"RecentRatio":f5/fall if fall>0 else None,
        "ExactTraceNodes":trace,"F5KnownSlotN":known,"F5KnownSlotCoverage":cov,"MaxKnownDepth5":mdepth,
        "F5CommonAncestorN":cn,"F5PathPairN":pn,"F5NearestCross":nc,"F5SourceHHI":hhi,
        "F5MaxSourceShare":mx,"F5StructureClass":cls,
        "StakesDBPresent":int(pk in stakes_present),"ScopeFlatStakesTop3":int(top3>0),"ScopeTop3N":top3,
        "ScopeWinN":wins,"ScopeAnyWin":int(wins>0),"WinConversion":wins/top3 if top3 else None,
        "RepeatWin2":int(wins>=2),"RepeatWin3":int(wins>=3),"RepeatWin5":int(wins>=5),
        "Top3_5plus":int(top3>=5),"Top3_8plus":int(top3>=8),"MultiYearTop3":int(o is not None and len(o.years)>=2),
        "Age2Top3":int(2 in ages),"Age3Top3":int(3 in ages),"Age4Top3":int(4 in ages),
        "Age5Top3":int(5 in ages),"Age6plusTop3":int(any(a>=6 for a in ages)),"Mature5plus":int(any(a>=5 for a in ages)),
        "FinalTop3Age":max(ages) if ages else None,
        "TurfTop3":int(o is not None and o.turf_top3_n>0),"TurfWin":int(o is not None and o.turf_win_n>0),
        "DirtTop3":int(o is not None and o.dirt_top3_n>0),"DirtWin":int(o is not None and o.dirt_win_n>0),
        "TurfDirtBoth":int(o is not None and o.turf_top3_n>0 and o.dirt_top3_n>0),
        "DistanceMulti2":int(dist_n>=2),"DistanceMulti3":int(dist_n>=3),
        "GradeEraEligible":grade_eligible,
        "G1Reach":(o.g1_reach if grade_eligible else None),"G1Win":(o.g1_win if grade_eligible else None),
        "G2Reach":(o.g2_reach if grade_eligible else None),"G2Win":(o.g2_win if grade_eligible else None),
        "G3Reach":(o.g3_reach if grade_eligible else None),"G3Win":(o.g3_win if grade_eligible else None),
        "ListedReach":(o.listed_reach if grade_eligible else None),
    }
    for k in DISTANCE_KEYS:
        rec[k+"Top3"]=int(o is not None and o.dist_top3[k]>0)
        rec[k+"Win"]=int(o is not None and o.dist_win[k]>0)
    rec.update(pos.get(pk,{}))
    return rec


def csv_value(v):
    return "" if v is None else v


def write_horse_csv(path,records):
    if not records:
        path.write_text("",encoding="utf-8-sig"); return
    fields=list(records[0].keys())
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in records: w.writerow({k:csv_value(r.get(k)) for k in fields})


def primary_pct(r,metric):
    v=r.get(metric+"CountryEraPct")
    return v if v is not None else r.get(metric+"GlobalEraPct")


def primary_z(r,metric):
    v=r.get(metric+"CountryEraZ")
    return v if v is not None else r.get(metric+"GlobalEraZ")


def band_of(p):
    if p is None: return ""
    for lo,hi,label in F_BANDS:
        if lo<=p<hi: return label
    return F_BANDS[-1][2]


BINARY_OUTCOMES=(
    "ScopeFlatStakesTop3","ScopeAnyWin","RepeatWin2","RepeatWin3","RepeatWin5","Top3_5plus","Top3_8plus",
    "MultiYearTop3","Age2Top3","Age3Top3","Age4Top3","Age5Top3","Age6plusTop3","Mature5plus",
    "TurfTop3","TurfWin","DirtTop3","DirtWin","TurfDirtBoth","DistanceMulti2","DistanceMulti3",
    "G1Reach","G1Win","G2Reach","G2Win","G3Reach","G3Win",
)+tuple(k+s for k in DISTANCE_KEYS for s in ("Top3","Win"))


def populations(records):
    base=[r for r in records if r["AnalysisPeriodIncluded"]]
    return {"BLOOD_ALL":base,"STAKES_TOP3":[r for r in base if r["ScopeFlatStakesTop3"]]}


def mean(vals):
    return sum(vals)/len(vals) if vals else None


def summarize_group(pop,metric,band,rows):
    rec={"Population":pop,"Metric":metric,"Band":band,"N":len(rows),
         "MetricMean":mean([r[metric] for r in rows if r.get(metric) is not None]),
         "Top3MeanN":mean([r["ScopeTop3N"] for r in rows]),"WinMeanN":mean([r["ScopeWinN"] for r in rows]),
         "WinConversionMean":mean([r["WinConversion"] for r in rows if r.get("WinConversion") is not None]),
         "GradeEligibleN":sum(1 for r in rows if r.get("GradeEraEligible")==1)}
    for o in BINARY_OUTCOMES:
        vals=[r[o] for r in rows if r.get(o) is not None]
        rec[o+"Rate"]=mean(vals)
    return rec


def write_era_bins(records,path):
    out=[]
    for pop,rr in populations(records).items():
        for metric in ("Fall","F5","DeepDelta","RecentRatio"):
            groups=defaultdict(list)
            for r in rr:
                p=primary_pct(r,metric)
                if p is not None: groups[band_of(p)].append(r)
            for _,_,b in F_BANDS:
                if groups.get(b): out.append(summarize_group(pop,metric,b,groups[b]))
    write_horse_csv(path,out)


def conditional_assignments(records,direction):
    outer_metric,inner_metric=("Fall","F5") if direction=="Fall固定→F5" else ("F5","Fall")
    cells=defaultdict(list)
    for r in records:
        if not r["AnalysisPeriodIncluded"]: continue
        p=primary_pct(r,outer_metric)
        if p is None or r.get(inner_metric) is None: continue
        cell=min(19,int(p*20))
        key=(r.get("BirthCountry") or "",r.get("Birth5"),cell)
        cells[key].append(r)
    assignment={}
    for key,g in cells.items():
        pairs=[(str(r["PrimaryKey"]),float(r[inner_metric])) for r in g]
        ranked=average_tie_pct_z(pairs)
        for r in g:
            p=ranked[str(r["PrimaryKey"])][0]
            assignment[r["PrimaryKey"]]="LOW" if p<1/3 else ("MID" if p<2/3 else "HIGH")
    return assignment


def write_conditional(records,path):
    out=[]; pops=populations(records)
    for direction in ("Fall固定→F5","F5固定→Fall"):
        assign=conditional_assignments(records,direction)
        for pop,rr in pops.items():
            for gn in ("LOW","MID","HIGH"):
                g=[r for r in rr if assign.get(r["PrimaryKey"])==gn]
                if not g: continue
                rec=summarize_group(pop,"F5" if direction.startswith("Fall") else "Fall",gn,g)
                rec["Direction"]=direction; rec["InnerGroup"]=gn
                rec["FallMean"]=mean([x["Fall"] for x in g]); rec["F5Mean"]=mean([x["F5"] for x in g]); rec["DeepDeltaMean"]=mean([x["DeepDelta"] for x in g])
                out.append(rec)
    write_horse_csv(path,out)


def sire_birth5_assignments(records,metric):
    cells=defaultdict(list)
    for r in records:
        if not r["AnalysisPeriodIncluded"] or not r.get("SirePK") or r.get("Birth5") is None: continue
        cells[(r["SirePK"],r["Birth5"])].append(r)
    assign={}; celln={}
    for key,g in cells.items():
        ranked=average_tie_pct_z([(str(r["PrimaryKey"]),float(r[metric])) for r in g])
        for r in g:
            p=ranked[str(r["PrimaryKey"])][0]
            assign[r["PrimaryKey"]]="LOW25" if p<.25 else ("MID50" if p<.75 else "HIGH25")
            celln[r["PrimaryKey"]]=len(g)
    return assign,celln


def write_sire_birth5(records,path):
    out=[]; pops=populations(records)
    for metric in ("Fall","F5","DeepDelta"):
        assign,celln=sire_birth5_assignments(records,metric)
        for pop,rr in pops.items():
            for gn in ("LOW25","MID50","HIGH25"):
                g=[r for r in rr if assign.get(r["PrimaryKey"])==gn]
                if not g: continue
                rec=summarize_group(pop,metric,gn,g)
                rec["SireBirth5Group"]=gn; rec["MeanSireBirth5CellN"]=mean([celln[r["PrimaryKey"]] for r in g])
                out.append(rec)
    write_horse_csv(path,out)


def write_structure(records,path):
    out=[]
    for pop,rr in populations(records).items():
        for axis in ("F5StructureClass","F5NearestCross"):
            groups=defaultdict(list)
            for r in rr: groups[str(r.get(axis) or "NONE")].append(r)
            for name,g in sorted(groups.items(),key=lambda kv:-len(kv[1])):
                rec=summarize_group(pop,"F5",name,g); rec["Axis"]=axis; rec["Group"]=name
                rec["FallMean"]=mean([x["Fall"] for x in g]); rec["DeepDeltaMean"]=mean([x["DeepDelta"] for x in g])
                out.append(rec)
    write_horse_csv(path,out)


def inv3(m):
    a,b,c=m[0]; d,e,f=m[1]; g,h,i=m[2]
    A=e*i-f*h; B=-(d*i-f*g); C=d*h-e*g
    D=-(b*i-c*h); E=a*i-c*g; F=-(a*h-b*g)
    G=b*f-c*e; H=-(a*f-c*d); I=a*e-b*d
    det=a*A+b*B+c*C
    if abs(det)<1e-14: return None
    return [[A/det,D/det,G/det],[B/det,E/det,H/det],[C/det,F/det,I/det]]


def fit_logit_quad(rows,metric,outcome):
    data=[]
    for r in rows:
        z=primary_z(r,metric); y=r.get(outcome)
        if z is not None and y is not None:
            data.append((float(z),int(y)))
    if len(data)<4:
        return None,"N<4"
    sy=sum(y for _,y in data)
    if sy==0 or sy==len(data):
        return None,"outcome constant"
    beta=[0.0,0.0,0.0]; H=None
    for _ in range(80):
        grad=[0.0,0.0,0.0]; H=[[0.0]*3 for _ in range(3)]
        for x,y in data:
            zz=(1.0,x,x*x); eta=sum(beta[j]*zz[j] for j in range(3)); eta=max(-35,min(35,eta))
            p=1.0/(1.0+math.exp(-eta)); rr=y-p; ww=max(1e-12,p*(1-p))
            for j in range(3):
                grad[j]+=zz[j]*rr
                for k in range(3): H[j][k]+=zz[j]*zz[k]*ww
        iv=inv3(H)
        if iv is None: return None,"singular"
        step=[sum(iv[j][k]*grad[k] for k in range(3)) for j in range(3)]
        beta=[beta[j]+step[j] for j in range(3)]
        if max(abs(x) for x in step)<1e-8: break
    iv=inv3(H)
    if iv is None: return None,"singular"
    se=[math.sqrt(max(0.0,iv[j][j])) for j in range(3)]
    p1=math.erfc(abs(beta[1]/se[1])/math.sqrt(2)) if se[1]>0 else None
    p2=math.erfc(abs(beta[2]/se[2])/math.sqrt(2)) if se[2]>0 else None
    vertex=-beta[1]/(2*beta[2]) if abs(beta[2])>1e-12 else None
    return {"N":len(data),"SuccessN":sy,"BetaZ":beta[1],"SEZ":se[1],"PZ":p1,"BetaZ2":beta[2],"SEZ2":se[2],"PZ2":p2,"VertexSD":vertex},"OK"


def write_logistic(records,path):
    out=[]; pops=populations(records)
    for pop,rr in pops.items():
        outcomes=("ScopeFlatStakesTop3","ScopeAnyWin") if pop=="BLOOD_ALL" else (
            "ScopeAnyWin","RepeatWin2","RepeatWin3","RepeatWin5","Top3_5plus","Top3_8plus","MultiYearTop3","Mature5plus",
            "G1Reach","G1Win","G2Reach","G2Win","G3Reach","G3Win")
        for metric in ("Fall","F5","DeepDelta","RecentRatio"):
            for outcome in outcomes:
                use=rr
                if outcome.startswith("G"):
                    use=[r for r in rr if r.get("GradeEraEligible")==1]
                fit,status=fit_logit_quad(use,metric,outcome)
                rec={"Population":pop,"Metric":metric,"Outcome":outcome,"Status":status,
                     "Note":"Vertexは観察曲線の頂点であり最適近交値ではない"}
                if fit: rec.update(fit)
                out.append(rec)
    write_horse_csv(path,out)


def write_report(path,records,scope,blood,stakes,start,end,eval_end):
    base=[r for r in records if r["AnalysisPeriodIncluded"]]
    stakes_pop=[r for r in base if r["ScopeFlatStakesTop3"]]
    grade_n=sum(1 for r in stakes_pop if r.get("GradeEraEligible")==1)
    txt=f"""近交係数 × 平地stakes 多軸監査\nVersion: {SCRIPT_VERSION}\n\n入力\n  blood: {blood}\n  stakes:\n"""
    txt += "\n".join("    "+str(x) for x in stakes)
    txt += f"""\n\n開催scope: {scope}\n対象馬出生年: {start if start is not None else 'ALL'} ～ {end if end is not None else 'ALL'}\n成績評価終了年: {eval_end if eval_end is not None else 'ALL'}\n\n母集団\n  blood全収録: {len(records):,}\n  分析期間blood: {len(base):,}\n  scope平地stakes Top3: {len(stakes_pop):,}\n  Grade-era eligible stakes馬: {grade_n:,}\n  人数による除外: なし\n\n重要な分母\n  BLOOD_ALLは公式な出走馬成功率ではない。\n  「blood DB収録馬のうち、選択scopeのstakes DBに成功記録が現れる割合」。\n  stakes未収録馬を『出走して負けた馬』とは扱わない。\n  StakesDBPresentとScopeFlatStakesTop3を別列で保存している。\n\n近交\n  Fall = blood全記録血統から内部計算した全世代Exact F\n  F5 = 対象馬から5世代で打ち切った局所pedigreeのExact F\n  DeepDelta = Fall-F5\n  RecentRatio = F5/Fall\n  F5KnownSlotCoverage = 5代62祖先枠のうち既知だった枠割合\n  F5SourceHHI/MaxSourceShare = simple-loop近交源集中proxy（厳密partial-Fではない）\n\nGrade制度\n  grade列だけを使用。series_gradeは代用しない。\n  USA 1973 / GB・IRE・FR 1971 / GER 1972 / NZ 1984、\n  JRA 1984 / NAR系 1997を開始年として、制度前の後付けGrade表記を無効化。\n  未定義国や制度前はG1/G2/G3を0ではなくNA。\n\n日本scope\n  JPN_ALL = JRA + NAR + track不明\n  JRA = 中央10場 + 明示的歴史別名\n  NAR = 日本開催でtrack非空かつJRA判定外\n  track不明はJRA/NARでは除外\n\n統計\n  EraPct/Zはblood全体の出生国×出生5年帯を優先し、出生国欠損時は全体birth5を使用。\n  同率はaverage-tie。N下限は置かない。\n  Fall固定→F5、F5固定→Fall、同父×出生5年帯、z+z^2を出力。\n\n解釈禁止\n  低F側低下を直ちにoutbreeding depressionと呼ばない。\n  高F側不利を『Fを下げるほど強い』へ変換しない。\n  二次曲線Vertexを育種上の最適Fとしない。\n"""
    path.write_text(txt,encoding="utf-8")


def self_test():
    ped={
        "A":PedigreeRec("","",1900,"","A",""), "B":PedigreeRec("","",1900,"","B",""), "C":PedigreeRec("","",1900,"","C",""),
        "H1":PedigreeRec("A","B",1910,"","H1",""), "H2":PedigreeRec("A","C",1911,"","H2",""),
        "X":PedigreeRec("H1","H2",1920,"","X",""),
        "F1":PedigreeRec("A","B",1910,"","F1",""), "F2":PedigreeRec("A","B",1911,"","F2",""),
        "Y":PedigreeRec("F1","F2",1920,"","Y",""),
        "P":PedigreeRec("A","B",1910,"","P",""), "Z":PedigreeRec("A","P",1920,"","Z",""),
    }
    order=topological_order(ped); idx,F,D,tr,s,d=compute_all_generation_exact(ped,order)
    assert abs(F[idx["X"]]-0.125)<1e-9, F[idx["X"]]
    assert abs(F[idx["Y"]]-0.25)<1e-9, F[idx["Y"]]
    assert abs(F[idx["Z"]]-0.25)<1e-9, F[idx["Z"]]
    fx=compute_local_exact_f5("X",ped,idx)[0]; fy=compute_local_exact_f5("Y",ped,idx)[0]; fz=compute_local_exact_f5("Z",ped,idx)[0]
    assert abs(fx-0.125)<1e-9, fx
    assert abs(fy-0.25)<1e-9, fy
    assert abs(fz-0.25)<1e-9, fz
    print("SELF TEST OK: half-sib=12.5%, full-sib=25%, parent-offspring=25% (Fall/F5)")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--base-dir",default=str(SCRIPT_DIR))
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:
        self_test(); return

    print("="*76)
    print("近交係数 × 平地stakes 多軸監査",SCRIPT_VERSION)
    print("必須入力: blood.csv + stakes_horses*.csv / 事前Exact F CSV不要")
    print("="*76)
    blood,stakes=discover_inputs(Path(args.base_dir).resolve())
    print("blood:",blood)
    print("stakes:"); [print(" ",x) for x in stakes]

    codes=choose_race_countries_pyto()
    japan_scope=choose_japan_scope_pyto() if codes==["JPN"] else "ALL"
    while True:
        try:
            start,end=parse_year_range_input(input("対象馬出生年 [空欄=全期間 / 例 1990-2018]: ").strip()); break
        except ValueError as e: print(e)
    while True:
        raw=input("成績評価終了年 [空欄=全記録 / 例 2026]: ").strip()
        if not raw: eval_end=None; break
        eval_end=parse_int(raw)
        if eval_end is not None: break
        print("4桁西暦で入力してください。")
    scope=analysis_scope_label(codes,japan_scope)
    print("開催scope:",scope)

    print("\n[1/7] blood読込")
    pedigree=read_pedigree(blood); print(f"  {len(pedigree):,}頭")
    print("\n[2/7] Fall/F5・5代構造（blood全頭）")
    metrics=load_or_compute_metrics(pedigree,blood,Path(args.base_dir)/CACHE_NAME)
    print("\n[3/7] stakes scan")
    outcomes,stakes_present=read_stakes(stakes,pedigree,codes,japan_scope,eval_end)
    print("\n[4/7] Era位置（blood全体基準）")
    raw,pos=build_era_positions(pedigree,metrics)
    print("\n[5/7] horse-level統合")
    records=[]
    for n,pk in enumerate(pedigree,1):
        records.append(horse_record(pk,pedigree,metrics,raw,pos,outcomes,stakes_present,start,end))
        if n%100000==0: print(f"  rows {n:,}/{len(pedigree):,}")

    period="allbirths" if start is None and end is None else f"{start or 'MIN'}-{end or 'MAX'}"
    if eval_end is not None: period+=f"_eval{eval_end}"
    tag=scope.replace(",","-")
    outdir=Path(args.base_dir)/f"inbreeding_scope_results_{tag}_{period}_{time.strftime('%Y%m%d_%H%M%S')}"
    outdir.mkdir(parents=True,exist_ok=True)

    print("\n[6/7] CSV出力")
    write_horse_csv(outdir/"inbreeding_all_blood_horses.csv",records)
    scope_stakes=[r for r in records if r["AnalysisPeriodIncluded"] and r["ScopeFlatStakesTop3"]]
    write_horse_csv(outdir/"inbreeding_scope_stakes_horses.csv",scope_stakes)
    write_era_bins(records,outdir/"inbreeding_era_bins.csv")
    write_conditional(records,outdir/"inbreeding_conditional_depth.csv")
    write_sire_birth5(records,outdir/"inbreeding_sire_birth5.csv")
    write_structure(records,outdir/"inbreeding_structure.csv")
    write_logistic(records,outdir/"inbreeding_logistic_quadratic.csv")

    print("\n[7/7] report / metadata")
    write_report(outdir/"inbreeding_report.txt",records,scope,blood,stakes,start,end,eval_end)
    meta={
        "version":SCRIPT_VERSION,"blood":str(blood),"stakes":[str(x) for x in stakes],"scope":scope,
        "race_country_codes":codes,"japan_scope":japan_scope,"birth_start":start,"birth_end":end,"evaluation_end":eval_end,
        "blood_horse_n":len(records),"analysis_period_blood_n":sum(r["AnalysisPeriodIncluded"] for r in records),
        "scope_stakes_top3_n":len(scope_stakes),"no_hidden_n_cutoff":True,
        "all_blood_output":True,"precomputed_exact_f_required":False,
        "grade_rule":"explicit grade only + historical grade-era validity; series_grade not substituted; pre-era/unknown -> NA",
        "blood_all_denominator_warning":"DB appearance, not starter-conditioned success probability",
    }
    (outdir/"run_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n完了:",outdir)
    print("まず見る: inbreeding_report.txt")
    print("全頭: inbreeding_all_blood_horses.csv")
    print("stakes成功馬: inbreeding_scope_stakes_horses.csv")


if __name__=="__main__":
    main()
