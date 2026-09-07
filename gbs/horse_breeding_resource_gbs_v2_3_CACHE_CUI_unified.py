#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GBS v2.3 CACHE + SQLITE + CUI unified entry point
==================================================

目的
----
- 実行入口をこの1本に固定する。
- 入力は原則 blood.csv と stakes_races_one_row.csv。
- 出力フォルダは常に gbs_explorer_cache。
- gbs_explorer_cache/gbs_explorer.sqlite3 があり、作成時の元データと現在の
  blood/stakes が同じなら、再計算せず即CUIへ入る。
- 元データが変わった、DBが壊れた、schemaが変わった場合は
  gbs_explorer_cache を丸ごと削除し、GBS v2.2計算 -> SQLite再構築 -> CUI。
- 巨大CSVはDB構築後に削除できる。通常研究はSQLite/CUIだけで行う。

重要
----
この版は「単一の実行入口」です。GBS/ratingの数値定義を変えないため、再構築時の
計算本体には既存の
  horse_breeding_resource_gbs_v2_2_DUAL_rating_all_in_one*.py
を自動検出して呼びます。v2.2の既存計算定義を別実装で書き直さないための安全策です。
一度SQLiteができれば、元CSVが変わらない限りv2.2計算本体は呼ばれません。

Pyto
----
このファイル、blood.csv、stakes_races_one_row.csv、v2.2計算本体を同じ
Documents/keiba に置いて実行してください。
初回は自動構築、2回目以降は即CUIです。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional


SCRIPT_VERSION = "2.3-cache-cui-unified-entry"
SCHEMA_VERSION = "3"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "gbs_explorer_cache"
DB_PATH = OUTPUT_DIR / "gbs_explorer.sqlite3"
BUILD_INFO_PATH = OUTPUT_DIR / "build_info.txt"
PAGE_SIZE = 30
MIN_COHORT_N = 20

# DB構築後、v2.2が作った巨大CSV/内部結果を削除して容量を戻す。
CLEAN_TEMP_V22_OUTPUTS = True

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2_147_483_647)


# ============================================================
# basic utilities
# ============================================================

def text(v) -> str:
    return unicodedata.normalize("NFKC", str(v or "")).strip()


def search_norm(v) -> str:
    s = text(v).lower().replace("+", " ")
    s = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def to_float(v):
    s = text(v)
    if not s or s.lower() in {"nan", "na", "none", "null"}:
        return None
    try:
        x = float(s)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def to_int(v):
    x = to_float(v)
    return int(x) if x is not None else None


def fmt_num(v, digits=2, empty="-"):
    if v is None:
        return empty
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return empty


def fmt_int(v, empty="-"):
    if v is None:
        return empty
    try:
        return f"{int(v):,}"
    except Exception:
        return empty


def fmt_pct(v, digits=1, empty="-"):
    if v is None:
        return empty
    try:
        x = float(v)
    except Exception:
        return empty
    if abs(x) <= 1.000001:
        x *= 100.0
    return f"{x:.{digits}f}%"


def line(ch="-", n=78):
    print(ch * n)


def heading(s):
    print()
    line("=")
    print(s)
    line("=")


def pause():
    input("\nEnterで戻る > ")


def safe_mean(vals):
    xs = [float(v) for v in vals if v is not None]
    return statistics.fmean(xs) if xs else None


def safe_median(vals):
    xs = [float(v) for v in vals if v is not None]
    return statistics.median(xs) if xs else None


def rankdata(values):
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i + 1
        while j < n and values[order[j]] == values[order[i]]:
            j += 1
        r = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = r
        i = j
    return ranks


def pearson(x, y):
    if len(x) < 3 or len(x) != len(y):
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    den = math.sqrt(sum(v*v for v in dx) * sum(v*v for v in dy))
    return sum(a*b for a, b in zip(dx, dy)) / den if den > 0 else None


def spearman(pairs):
    z = [(float(a), float(b)) for a, b in pairs if a is not None and b is not None]
    if len(z) < 3:
        return None
    a = [x for x, _ in z]
    b = [y for _, y in z]
    return pearson(rankdata(a), rankdata(b))


# ============================================================
# input discovery + fingerprint
# ============================================================

def newest(patterns, exclude_output=True):
    found = []
    for pat in patterns:
        for p in BASE_DIR.glob(pat):
            if p.is_file():
                if exclude_output and OUTPUT_DIR in p.parents:
                    continue
                found.append(p)
    return max(found, key=lambda p: p.stat().st_mtime_ns) if found else None


def discover_inputs():
    blood = newest(["blood.csv", "blood*.csv"])
    stakes = newest(["stakes_races_one_row.csv", "stakes_races_one_row*.csv"])
    if blood is None:
        raise FileNotFoundError("blood*.csv が見つかりません")
    if stakes is None:
        raise FileNotFoundError("stakes_races_one_row*.csv が見つかりません")
    return blood.resolve(), stakes.resolve()


def sampled_sha256(path: Path, chunk=1024*1024):
    """起動時の高速指紋。先頭・中央・末尾を読む。size/mtimeも別に保存する。"""
    h = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as f:
        offsets = [0]
        if size > chunk:
            offsets.append(max(0, size // 2 - chunk // 2))
            offsets.append(max(0, size - chunk))
        seen = set()
        for off in offsets:
            if off in seen:
                continue
            seen.add(off)
            f.seek(off)
            h.update(str(off).encode())
            h.update(f.read(chunk))
    return h.hexdigest()


def file_signature(path: Path):
    st = path.stat()
    return {
        "name": path.name,
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "sample_sha256": sampled_sha256(path),
    }


def current_source_signature(blood: Path, stakes: Path):
    return {
        "blood": file_signature(blood),
        "stakes": file_signature(stakes),
    }


# ============================================================
# cache validation / deletion
# ============================================================

def remove_output_dir():
    if OUTPUT_DIR.exists():
        print(f"旧キャッシュ全削除: {OUTPUT_DIR}", flush=True)
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def read_metadata(conn):
    try:
        return dict(conn.execute("SELECT key,value FROM metadata"))
    except sqlite3.Error:
        return {}


def db_is_valid(sig) -> tuple[bool, str]:
    if not DB_PATH.exists():
        return False, "DBなし"
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            md = read_metadata(conn)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"metadata", "horses", "horse_ratings"}.issubset(tables):
                return False, "必須table不足"
            if md.get("schema_version") != SCHEMA_VERSION:
                return False, "schema version変更"
            if md.get("source_signature") != json.dumps(sig, sort_keys=True, ensure_ascii=False):
                return False, "元データ変更"
            req = {"horse_pk", "horse_name", "sire_pk", "dam_pk", "old_gbs_pct", "dn_gbs_pct", "peak_rating"}
            if not req.issubset(table_columns(conn, "horses")):
                return False, "horses schema不足"
            conn.execute("SELECT COUNT(*) FROM horses").fetchone()
            return True, "OK"
        finally:
            conn.close()
    except Exception as exc:
        return False, f"DB破損/読込失敗: {exc}"


# ============================================================
# v2.2 calculation orchestration
# ============================================================

def find_v22_engine():
    patterns = [
        "horse_breeding_resource_gbs_v2_2_DUAL_rating_all_in_one*.py",
        "horse_breeding_resource_gbs_v2_2*.py",
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(p for p in BASE_DIR.glob(pat) if p.is_file() and p.resolve() != Path(__file__).resolve())
    return max(candidates, key=lambda p: p.stat().st_mtime_ns) if candidates else None


def locate_v22_outputs(after_epoch=0.0):
    horses = []
    ratings = []
    summaries = []
    for p in BASE_DIR.rglob("gbs_v2_2_horses*.csv"):
        if OUTPUT_DIR not in p.parents and p.stat().st_mtime >= after_epoch:
            horses.append(p)
    for p in BASE_DIR.rglob("grade_free_horse_ratings*.csv"):
        if OUTPUT_DIR not in p.parents and p.stat().st_mtime >= after_epoch:
            ratings.append(p)
    for p in BASE_DIR.rglob("gbs_v2_2_summary*.txt"):
        if OUTPUT_DIR not in p.parents and p.stat().st_mtime >= after_epoch:
            summaries.append(p)
    return (
        max(horses, key=lambda p: p.stat().st_mtime_ns) if horses else None,
        max(ratings, key=lambda p: p.stat().st_mtime_ns) if ratings else None,
        max(summaries, key=lambda p: p.stat().st_mtime_ns) if summaries else None,
    )


def run_v22_engine():
    engine = find_v22_engine()
    if engine is None:
        # 初回だけ既存v2.2 CSVがあるならDB化は可能。
        h, r, s = locate_v22_outputs(0.0)
        if h is not None:
            print("v2.2本体なし。既存v2.2出力からSQLiteを構築します。")
            return h, r, s, []
        raise FileNotFoundError(
            "再構築用 v2.2計算本体が見つかりません。\n"
            "horse_breeding_resource_gbs_v2_2_DUAL_rating_all_in_one.py を同じフォルダに置いてください。"
        )

    print(f"GBS v2.2再計算: {engine.name}", flush=True)
    before = time.time() - 2.0
    proc = subprocess.run([sys.executable, str(engine)], cwd=str(BASE_DIR))
    if proc.returncode != 0:
        raise RuntimeError(f"v2.2計算失敗 returncode={proc.returncode}")
    h, r, s = locate_v22_outputs(before)
    if h is None:
        # engineがmtimeを維持する特殊ケースに備える
        h, r, s = locate_v22_outputs(0.0)
    if h is None:
        raise FileNotFoundError("v2.2計算後に gbs_v2_2_horses.csv を発見できません")

    # cleanup対象は、v2.2出力を含む既知のresult directoryのみ。
    cleanup_dirs = set()
    for p in (h, r, s):
        if p is not None and p.parent != BASE_DIR:
            name = p.parent.name.lower()
            if "gbs_v2_2" in name or "dual" in name:
                cleanup_dirs.add(p.parent)
    return h, r, s, sorted(cleanup_dirs)


# ============================================================
# CSV -> SQLite
# ============================================================

def find_col(fields, aliases, required=False):
    norm = {search_norm(x).replace(" ", ""): x for x in fields if x}
    for a in aliases:
        k = search_norm(a).replace(" ", "")
        if k in norm:
            return norm[k]
    if required:
        raise ValueError(f"必須列なし aliases={aliases}; columns={fields}")
    return None


def open_csv(path):
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            f = path.open("r", encoding=enc, newline="")
            f.readline(); f.seek(0)
            return f
        except UnicodeDecodeError:
            try: f.close()
            except Exception: pass
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def create_schema(conn):
    conn.executescript("""
    DROP TABLE IF EXISTS metadata;
    DROP TABLE IF EXISTS horses;
    DROP TABLE IF EXISTS horse_ratings;

    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE horses(
      horse_pk TEXT PRIMARY KEY,
      horse_name TEXT,
      name_search TEXT,
      birth_year INTEGER,
      country TEXT,
      sire_pk TEXT,
      dam_pk TEXT,
      old_gbs_raw REAL,
      old_gbs_pct REAL,
      old_gbs_z REAL,
      dn_gbs_raw REAL,
      dn_gbs_pct REAL,
      dn_gbs_z REAL,
      dn_minus_old_pct REAL,
      old_relative_rated_n INTEGER,
      dn_relative_rated_n INTEGER,
      old_relative_coverage REAL,
      dn_relative_coverage REAL,
      peak_rating REAL,
      peak_rating_year INTEGER,
      peak_global_rank INTEGER,
      peak_global_pct REAL,
      peak_confidence REAL,
      peak_dynamic_rank INTEGER,
      peak_dynamic_pct REAL,
      peak_dynamic_horses INTEGER,
      peak_dynamic_country TEXT,
      peak_dynamic_surface TEXT
    );

    CREATE TABLE horse_ratings(
      horse_pk TEXT NOT NULL,
      year INTEGER NOT NULL,
      horse_name TEXT,
      annual_rating REAL,
      global_rank INTEGER,
      global_pct REAL,
      component_rank INTEGER,
      component_pct REAL,
      horse_confidence REAL,
      PRIMARY KEY(horse_pk, year)
    ) WITHOUT ROWID;
    """)


def import_horses(conn, path):
    print(f"SQLite horses取込: {path}", flush=True)
    with open_csv(path) as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []
        A = {
            "pk": ["horse_pk", "PrimaryKey", "pk"],
            "name": ["horse_name", "Horse Name", "name"],
            "birth": ["birth_year", "birth", "Year"],
            "country": ["country", "Country"],
            "sire": ["sire", "sire_pk", "Sire"],
            "dam": ["dam", "dam_pk", "Dam"],
            "oraw": ["old_gbs_raw"], "opct": ["era_old_gbs_pct"], "oz": ["era_old_gbs_z"],
            "draw": ["dn_gbs_raw"], "dpct": ["era_dn_gbs_pct"], "dz": ["era_dn_gbs_z"],
            "gap": ["era_dn_minus_old_pct"],
            "orn": ["old_relative_rated_n"], "drn": ["dn_relative_rated_n"],
            "ocov": ["old_relative_coverage"], "dcov": ["dn_relative_coverage"],
            "peak": ["own_peak_annual_horse_rating"], "pyear": ["own_peak_rating_year"],
            "prank": ["own_peak_annual_global_rank"], "ppct": ["own_peak_annual_global_percentile"],
            "pconf": ["own_peak_horse_confidence"], "drank": ["own_peak_dynamic_network_rank"],
            "dnpct": ["own_peak_dynamic_network_percentile"], "dnh": ["own_peak_dynamic_network_horses"],
            "dnc": ["own_peak_dynamic_dominant_country"], "dns": ["own_peak_dynamic_dominant_surface"],
        }
        cols = {k: find_col(fields, v, required=(k == "pk")) for k, v in A.items()}
        def rv(row, k):
            c = cols.get(k); return row.get(c, "") if c else ""
        sql = """INSERT OR REPLACE INTO horses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        batch=[]; n=0
        for row in rd:
            pk=text(rv(row,"pk"))
            if not pk: continue
            name=text(rv(row,"name")) or pk
            batch.append((
                pk,name,search_norm(name),to_int(rv(row,"birth")),text(rv(row,"country")),
                text(rv(row,"sire")),text(rv(row,"dam")),
                to_float(rv(row,"oraw")),to_float(rv(row,"opct")),to_float(rv(row,"oz")),
                to_float(rv(row,"draw")),to_float(rv(row,"dpct")),to_float(rv(row,"dz")),to_float(rv(row,"gap")),
                to_int(rv(row,"orn")),to_int(rv(row,"drn")),to_float(rv(row,"ocov")),to_float(rv(row,"dcov")),
                to_float(rv(row,"peak")),to_int(rv(row,"pyear")),to_int(rv(row,"prank")),to_float(rv(row,"ppct")),
                to_float(rv(row,"pconf")),to_int(rv(row,"drank")),to_float(rv(row,"dnpct")),to_int(rv(row,"dnh")),
                text(rv(row,"dnc")),text(rv(row,"dns")),
            ))
            n+=1
            if len(batch)>=5000:
                conn.executemany(sql,batch); batch.clear()
                if n % 50000 < 5000: print(f"  {n:,}頭", flush=True)
        if batch: conn.executemany(sql,batch)
    return n


def import_ratings(conn, path):
    if path is None:
        print("年別horse rating CSVなし。peak ratingのみ閲覧します。")
        return 0
    print(f"SQLite horse_ratings取込: {path}", flush=True)
    with open_csv(path) as f:
        rd=csv.DictReader(f); fields=rd.fieldnames or []
        A={
            "pk":["horse_pk","PrimaryKey","pk"], "year":["year"], "name":["horse_name","Horse Name"],
            "rating":["annual_horse_rating","annual_rating"], "grank":["annual_global_rank","global_rank"],
            "gpct":["annual_global_percentile","global_percentile"], "crank":["component_rank"],
            "cpct":["component_percentile"], "conf":["horse_confidence"],
        }
        cols={k:find_col(fields,v,required=(k in {"pk","year","rating"})) for k,v in A.items()}
        def rv(row,k): c=cols.get(k); return row.get(c,"") if c else ""
        sql="INSERT OR REPLACE INTO horse_ratings VALUES(?,?,?,?,?,?,?,?,?)"
        batch=[]; n=0
        for row in rd:
            pk=text(rv(row,"pk")); year=to_int(rv(row,"year")); rat=to_float(rv(row,"rating"))
            if not pk or year is None or rat is None: continue
            batch.append((pk,year,text(rv(row,"name")),rat,to_int(rv(row,"grank")),to_float(rv(row,"gpct")),
                          to_int(rv(row,"crank")),to_float(rv(row,"cpct")),to_float(rv(row,"conf"))))
            n+=1
            if len(batch)>=5000: conn.executemany(sql,batch); batch.clear()
        if batch: conn.executemany(sql,batch)
    return n


def build_database(sig, horse_csv, rating_csv, summary_path):
    tmp = OUTPUT_DIR / "gbs_explorer.sqlite3.building"
    if tmp.exists(): tmp.unlink()
    conn=sqlite3.connect(str(tmp))
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-64000")
        create_schema(conn)
        hn=import_horses(conn,horse_csv)
        rn=import_ratings(conn,rating_csv)
        conn.executescript("""
          CREATE INDEX idx_horses_name ON horses(name_search);
          CREATE INDEX idx_horses_sire ON horses(sire_pk);
          CREATE INDEX idx_horses_dam ON horses(dam_pk);
          CREATE INDEX idx_horses_birth ON horses(birth_year);
          CREATE INDEX idx_horses_oldpct ON horses(old_gbs_pct);
          CREATE INDEX idx_horses_dnpct ON horses(dn_gbs_pct);
          CREATE INDEX idx_horses_rating ON horses(peak_rating);
          CREATE INDEX idx_ratings_year ON horse_ratings(year);
          CREATE INDEX idx_ratings_rating ON horse_ratings(annual_rating);
        """)
        summary_text=""
        if summary_path and summary_path.exists():
            summary_text=summary_path.read_text(encoding="utf-8-sig",errors="replace")[:200000]
        md={
            "schema_version":SCHEMA_VERSION,
            "script_version":SCRIPT_VERSION,
            "source_signature":json.dumps(sig,sort_keys=True,ensure_ascii=False),
            "horse_count":str(hn),"rating_row_count":str(rn),
            "built_epoch":str(time.time()),"source_summary":summary_text,
        }
        conn.executemany("INSERT INTO metadata(key,value) VALUES(?,?)",md.items())
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp,DB_PATH)
    conn=sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("ANALYZE")
        conn.commit()
    finally: conn.close()
    BUILD_INFO_PATH.write_text(
        "GBS explorer cache\n"+
        json.dumps({"script":SCRIPT_VERSION,"schema":SCHEMA_VERSION,"source":sig,
                    "horse_csv":str(horse_csv),"rating_csv":str(rating_csv or "")},
                   ensure_ascii=False,indent=2), encoding="utf-8"
    )


def cleanup_v22(horse_csv, rating_csv, summary_path, cleanup_dirs):
    if not CLEAN_TEMP_V22_OUTPUTS: return
    # root直下にあるユーザー既存CSVは勝手に消さない。専用result dirだけ削除。
    for d in cleanup_dirs:
        try:
            if d.exists() and d != OUTPUT_DIR and d.parent == BASE_DIR:
                print(f"一時v2.2出力削除: {d.name}", flush=True)
                shutil.rmtree(d)
        except Exception as exc:
            print(f"一時出力削除warning: {exc}")


# ============================================================
# DB helpers
# ============================================================

def connect_db():
    c=sqlite3.connect(str(DB_PATH)); c.row_factory=sqlite3.Row
    c.execute("PRAGMA cache_size=-32000")
    return c


def get_horse(c, pk):
    return c.execute("SELECT * FROM horses WHERE horse_pk=?",(pk,)).fetchone()


def parent_name(c, pk):
    if not pk: return "-"
    r=get_horse(c,pk); return (r["horse_name"] if r else pk) or pk


def children(c, horse):
    pk=horse["horse_pk"]
    # 種牡馬/繁殖牝馬どちらでも子へ潜れるよう父母両側
    return c.execute("SELECT * FROM horses WHERE sire_pk=? OR dam_pk=?",(pk,pk)).fetchall()


# ============================================================
# CUI displays
# ============================================================

def show_horse(c,h):
    heading(f"{h['horse_name'] or h['horse_pk']} [{h['birth_year'] or '?'}] <{h['horse_pk']}>")
    print(f"国 : {h['country'] or '-'}")
    print(f"父 : {parent_name(c,h['sire_pk'])} <{h['sire_pk'] or '-'}>")
    print(f"母 : {parent_name(c,h['dam_pk'])} <{h['dam_pk'] or '-'}>")
    print("\n[出生時繁殖資源]")
    print(f"Old GBS : raw={fmt_num(h['old_gbs_raw'],4)} pct={fmt_pct(h['old_gbs_pct'],2)} z={fmt_num(h['old_gbs_z'],3)}")
    print(f"DN-GBS  : raw={fmt_num(h['dn_gbs_raw'],4)} pct={fmt_pct(h['dn_gbs_pct'],2)} z={fmt_num(h['dn_gbs_z'],3)}")
    print(f"DN-Old  : {fmt_pct(h['dn_minus_old_pct'],2)}")
    print(f"coverage: Old={fmt_pct(h['old_relative_coverage'],1)} / DN={fmt_pct(h['dn_relative_coverage'],1)}")
    print("\n[本人能力]")
    print(f"Peak Rating : {fmt_num(h['peak_rating'],4)} / year={fmt_int(h['peak_rating_year'])}")
    print(f"Global      : rank={fmt_int(h['peak_global_rank'])} / pct={fmt_pct(h['peak_global_pct'],2)}")
    print(f"Dynamic     : rank={fmt_int(h['peak_dynamic_rank'])} / pct={fmt_pct(h['peak_dynamic_pct'],2)} / N={fmt_int(h['peak_dynamic_horses'])}")
    print(f"Network     : {h['peak_dynamic_country'] or '-'} / {h['peak_dynamic_surface'] or '-'}")
    n=c.execute("SELECT COUNT(*) FROM horses WHERE sire_pk=? OR dam_pk=?",(h['horse_pk'],h['horse_pk'])).fetchone()[0]
    print(f"\n産駒: {n:,}頭")


def progeny_summary(c,h):
    rows=children(c,h)
    heading(f"{h['horse_name']} 産駒サマリー")
    old=[r['old_gbs_pct'] for r in rows if r['old_gbs_pct'] is not None]
    dn=[r['dn_gbs_pct'] for r in rows if r['dn_gbs_pct'] is not None]
    rated=[r for r in rows if r['peak_rating'] is not None]
    ratings=[r['peak_rating'] for r in rated]
    print(f"産駒={len(rows):,} / Ratingあり={len(rated):,}")
    print(f"Old GBS 平均/中央値={fmt_pct(safe_mean(old),2)} / {fmt_pct(safe_median(old),2)}")
    print(f"DN-GBS  平均/中央値={fmt_pct(safe_mean(dn),2)} / {fmt_pct(safe_median(dn),2)}")
    print(f"Rating  平均/中央値={fmt_num(safe_mean(ratings),3)} / {fmt_num(safe_median(ratings),3)}")
    for cut in (105,110,115,120):
        print(f"Rating {cut}+ = {sum(1 for x in ratings if x>=cut):,}")
    print(f"Old GBS × Rating rho={fmt_num(spearman([(r['old_gbs_pct'],r['peak_rating']) for r in rated]),4)}")
    print(f"DN-GBS  × Rating rho={fmt_num(spearman([(r['dn_gbs_pct'],r['peak_rating']) for r in rated]),4)}")


def print_child_rows(rows, offset=0):
    print(f"{'No':>3} {'馬名':<28} {'生年':>5} {'Old':>8} {'DN':>8} {'Rating':>9}")
    line()
    for i,r in enumerate(rows,start=offset+1):
        name=(r['horse_name'] or r['horse_pk'])[:28]
        print(f"{i:>3} {name:<28} {fmt_int(r['birth_year']):>5} {fmt_pct(r['old_gbs_pct'],1):>8} {fmt_pct(r['dn_gbs_pct'],1):>8} {fmt_num(r['peak_rating'],2):>9}")


def ordered_children(rows, mode):
    if mode=="1": return sorted(rows,key=lambda r:(r['peak_rating'] is not None,r['peak_rating'] or -1e9),reverse=True)
    if mode=="2": return sorted(rows,key=lambda r:(r['dn_gbs_pct'] is not None,r['dn_gbs_pct'] or -1),reverse=True)
    if mode=="3": return sorted(rows,key=lambda r:(r['old_gbs_pct'] is not None,r['old_gbs_pct'] or -1),reverse=True)
    if mode=="4": return sorted(rows,key=lambda r:(r['dn_gbs_pct'] is None,r['dn_gbs_pct'] if r['dn_gbs_pct'] is not None else 9))
    return sorted(rows,key=lambda r:(r['birth_year'] is None,r['birth_year'] or 9999,r['horse_name'] or ""))


def choose_child(c,h):
    rows=children(c,h)
    if not rows:
        print("産駒なし"); pause(); return None
    print("[1] Rating高い順  [2] DN高い順  [3] Old高い順  [4] DN低い順  [5] 出生年順")
    mode=input("並び順 > ").strip() or "1"
    rows=ordered_children(rows,mode); page=0
    while True:
        st=page*PAGE_SIZE; en=min(len(rows),st+PAGE_SIZE)
        heading(f"{h['horse_name']} 産駒 {st+1}-{en}/{len(rows)}")
        print_child_rows(rows[st:en],st)
        cmd=input("\n番号=その馬 / n=次 / p=前 / b=戻る > ").strip().lower()
        if cmd=="b": return None
        if cmd=="n" and en<len(rows): page+=1; continue
        if cmd=="p" and page>0: page-=1; continue
        try: idx=int(cmd)-1
        except Exception: continue
        if 0<=idx<len(rows): return rows[idx]


def trend(c,h):
    rows=children(c,h); rated=[r for r in rows if r['peak_rating'] is not None]
    heading(f"{h['horse_name']} GBS × Rating傾向")
    print(f"Old rho={fmt_num(spearman([(r['old_gbs_pct'],r['peak_rating']) for r in rated]),4)}")
    print(f"DN  rho={fmt_num(spearman([(r['dn_gbs_pct'],r['peak_rating']) for r in rated]),4)}")
    print("\nDN帯")
    bins=[("LOW <40%",lambda x:x<.40),("MID 40-75%",lambda x:.40<=x<.75),("HIGH >=75%",lambda x:x>=.75)]
    for label,fn in bins:
        z=[r for r in rows if r['dn_gbs_pct'] is not None and fn(r['dn_gbs_pct'])]
        rr=[r['peak_rating'] for r in z if r['peak_rating'] is not None]
        print(f"{label:<14} N={len(z):>4} rated={len(rr):>4} avgR={fmt_num(safe_mean(rr),3):>8} 110+={sum(1 for x in rr if x>=110):>3}")


def representatives(c,h):
    rows=[r for r in children(c,h) if r['peak_rating'] is not None]
    heading(f"{h['horse_name']} 代表産駒")
    print("[Rating上位]"); print_child_rows(sorted(rows,key=lambda r:r['peak_rating'],reverse=True)[:20])
    print("\n[低DN(<40%)からの高Rating]")
    low=sorted([r for r in rows if r['dn_gbs_pct'] is not None and r['dn_gbs_pct']<.40],key=lambda r:r['peak_rating'],reverse=True)
    if low: print_child_rows(low[:20])
    else: print("該当なし")


def birth_trend(c,h):
    rows=children(c,h); groups={}
    for r in rows:
        y=r['birth_year']
        if y is None: continue
        b=(y//5)*5; groups.setdefault(b,[]).append(r)
    heading(f"{h['horse_name']} 産駒出生5年帯")
    print(f"{'Birth5':<11}{'N':>6}{'Old':>10}{'DN':>10}{'Rated':>8}{'Rating':>10}{'110+':>7}"); line()
    for b in sorted(groups):
        z=groups[b]; old=[r['old_gbs_pct'] for r in z if r['old_gbs_pct'] is not None]; dn=[r['dn_gbs_pct'] for r in z if r['dn_gbs_pct'] is not None]; rr=[r['peak_rating'] for r in z if r['peak_rating'] is not None]
        print(f"{b}-{b+4:<5}{len(z):>6}{fmt_pct(safe_mean(old),1):>10}{fmt_pct(safe_mean(dn),1):>10}{len(rr):>8}{fmt_num(safe_mean(rr),2):>10}{sum(1 for x in rr if x>=110):>7}")


def rating_history(c,h):
    rows=c.execute("SELECT * FROM horse_ratings WHERE horse_pk=? ORDER BY year",(h['horse_pk'],)).fetchall()
    heading(f"{h['horse_name']} 年別Rating")
    if not rows: print("年別ratingなし"); return
    print(f"{'Year':>6}{'Rating':>10}{'Global':>10}{'G.Pct':>10}{'Comp':>10}{'C.Pct':>10}{'Conf':>10}"); line()
    for r in rows:
        print(f"{r['year']:>6}{fmt_num(r['annual_rating'],3):>10}{fmt_int(r['global_rank']):>10}{fmt_pct(r['global_pct'],1):>10}{fmt_int(r['component_rank']):>10}{fmt_pct(r['component_pct'],1):>10}{fmt_num(r['horse_confidence'],3):>10}")


def mates(c,h):
    pk=h['horse_pk']
    rows=c.execute("SELECT *, CASE WHEN sire_pk=? THEN dam_pk ELSE sire_pk END AS mate_pk FROM horses WHERE sire_pk=? OR dam_pk=?",(pk,pk,pk)).fetchall()
    heading(f"{h['horse_name']} 配合相手")
    rows=sorted(rows,key=lambda r:(r['peak_rating'] is not None,r['peak_rating'] or -1e9),reverse=True)
    print(f"{'産駒':<24}{'相手':<24}{'Old':>8}{'DN':>8}{'Rating':>9}"); line()
    for r in rows[:60]:
        m=get_horse(c,r['mate_pk']); mn=(m['horse_name'] if m else r['mate_pk']) or '-'
        print(f"{(r['horse_name'] or r['horse_pk'])[:24]:<24}{mn[:24]:<24}{fmt_pct(r['old_gbs_pct'],1):>8}{fmt_pct(r['dn_gbs_pct'],1):>8}{fmt_num(r['peak_rating'],2):>9}")


# ============================================================
# search + navigation
# ============================================================

def search_horses(c,q,limit=50):
    q=text(q)
    exact=c.execute("SELECT * FROM horses WHERE lower(horse_pk)=lower(?) LIMIT 1",(q,)).fetchone()
    if exact: return [exact]
    nq=search_norm(q); like="%"+nq+"%"
    return c.execute("SELECT * FROM horses WHERE name_search LIKE ? OR lower(horse_pk) LIKE lower(?) ORDER BY CASE WHEN name_search=? THEN 0 ELSE 1 END,birth_year,horse_name LIMIT ?",(like,"%"+q+"%",nq,limit)).fetchall()


def select_search(c):
    while True:
        q=input("\n馬名/PrimaryKey（q=終了） > ").strip()
        if q.lower()=="q": return "QUIT"
        if q.lower()=="!rebuild": return "REBUILD"
        rows=search_horses(c,q)
        if not rows: print("該当なし"); continue
        if len(rows)==1: return rows[0]
        heading(f"検索結果: {q}")
        for i,r in enumerate(rows,1):
            print(f"[{i:>2}] {r['horse_name'] or r['horse_pk']} [{r['birth_year'] or '?'}] <{r['horse_pk']}> Old={fmt_pct(r['old_gbs_pct'],1)} DN={fmt_pct(r['dn_gbs_pct'],1)} R={fmt_num(r['peak_rating'],2)}")
        try: n=int(input("選択(0=再検索) > ").strip())
        except Exception: continue
        if n==0: continue
        if 1<=n<=len(rows): return rows[n-1]


def horse_menu(c,horse):
    hist=[]
    while horse is not None:
        show_horse(c,horse)
        print("\n[1] 本馬 [2] 産駒summary [3] 産駒一覧 [4] GBS×Rating傾向 [5] 代表産駒")
        print("[6] 配合相手 [7] 出生5年帯 [8] 年別Rating [9] 父へ [10] 母へ [11] 産駒へ")
        print("[s] 新規検索 [b] 戻る [q] 終了")
        cmd=input("選択 > ").strip().lower()
        if cmd=="q": return "QUIT"
        if cmd=="s": return None
        if cmd=="b":
            if hist: horse=hist.pop()
            else: return None
            continue
        if cmd=="1": pause(); continue
        if cmd=="2": progeny_summary(c,horse); pause(); continue
        if cmd=="3" or cmd=="11":
            ch=choose_child(c,horse)
            if ch: hist.append(horse); horse=ch
            continue
        if cmd=="4": trend(c,horse); pause(); continue
        if cmd=="5": representatives(c,horse); pause(); continue
        if cmd=="6": mates(c,horse); pause(); continue
        if cmd=="7": birth_trend(c,horse); pause(); continue
        if cmd=="8": rating_history(c,horse); pause(); continue
        if cmd in {"9","10"}:
            pk=horse['sire_pk'] if cmd=="9" else horse['dam_pk']
            p=get_horse(c,pk) if pk else None
            if p: hist.append(horse); horse=p
            else: print("DBに親がありません"); pause()
            continue


def explorer():
    c=connect_db()
    try:
        while True:
            heading("GBS / HORSE RATING EXPLORER")
            print("通常は馬名/PKを入力。強制再構築は !rebuild")
            h=select_search(c)
            if h=="QUIT": return "QUIT"
            if h=="REBUILD": return "REBUILD"
            res=horse_menu(c,h)
            if res=="QUIT": return "QUIT"
    finally: c.close()


# ============================================================
# rebuild + main
# ============================================================

def rebuild(sig):
    remove_output_dir()
    horse_csv=rating_csv=summary_path=None; cleanup_dirs=[]
    try:
        horse_csv,rating_csv,summary_path,cleanup_dirs=run_v22_engine()
        build_database(sig,horse_csv,rating_csv,summary_path)
        print(f"SQLite完成: {DB_PATH}", flush=True)
    except Exception:
        # 中途半端DBを残さない
        if OUTPUT_DIR.exists(): shutil.rmtree(OUTPUT_DIR,ignore_errors=True)
        raise
    finally:
        if horse_csv is not None:
            cleanup_v22(horse_csv,rating_csv,summary_path,cleanup_dirs)


def main():
    blood,stakes=discover_inputs()
    sig=current_source_signature(blood,stakes)
    valid,reason=db_is_valid(sig)
    if valid:
        print("既存SQLiteは元データと一致。即CUIへ移行します。")
    else:
        print(f"SQLite再構築が必要: {reason}")
        rebuild(sig)

    while True:
        action=explorer()
        if action!="REBUILD": break
        # CUIから明示強制再構築。元データが同じでも全消去して作り直す。
        blood,stakes=discover_inputs(); sig=current_source_signature(blood,stakes)
        rebuild(sig)


if __name__ == "__main__":
    main()
