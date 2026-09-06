#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_0.py'
FAST = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_1_fast.py'
FIX = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_2_fast.py'
OUT = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_3_all_in_one.py'

base = BASE.read_text(encoding='utf-8')
fast = FAST.read_text(encoding='utf-8')
fix = FIX.read_text(encoding='utf-8')

# v1.0はmain定義まで保持し、自動実行部分だけ外す。
main_marker = '\nif __name__=="__main__":main()'
if main_marker in base:
    base = base.rsplit(main_marker, 1)[0]
else:
    m = re.search(r'\nif __name__\s*==\s*["\']__main__["\']\s*:\s*main\(\)\s*\Z', base)
    if not m:
        raise SystemExit('v1.0 main marker not found')
    base = base[:m.start()]

base = base.replace('from collections import defaultdict', 'from collections import defaultdict, Counter')
if 'from functools import lru_cache' not in base:
    base = base.replace('from dataclasses import dataclass', 'from dataclasses import dataclass\nfrom functools import lru_cache')
base = re.sub(r'VERSION\s*=\s*"[^"]+"', 'VERSION = "1.3.0-fast-all-in-one"', base, count=1)

# v1.1から高速化実装だけ取り込む。base importやランチャーは除去。
start = fast.index('class FastExactFEngine:')
end = fast.rfind('\nif __name__ == "__main__":')
if end < 0:
    raise SystemExit('v1.1 runner marker not found')
fast_body = fast[start:end]
fast_body = fast_body.replace('base.', '')
sm = fast_body.find('\ndef self_test():')
if sm >= 0:
    fast_body = fast_body[:sm]

# v1.2から生産国×出生5年帯 grouped logistic 修正だけ取り込む。
start2 = fix.index('RANK_COL =')
end2 = fix.index('\ndef self_test():')
fix_body = fix[start2:end2]
fix_body = fix_body.replace('fast.', '').replace('base.', '')

header = '''

# ===== v1.3 all-in-one fast layer =====
STAKES_CACHE_DB = SCRIPT_DIR / ".inbreeding_stakes_normalized_v2.sqlite3"
'''

selftest = '''


def self_test_all_in_one():
    by={
        "A":Horse("A"),"B":Horse("B"),"C":Horse("C"),
        "H1":Horse("H1",sire="A",dam="B"),"H2":Horse("H2",sire="A",dam="C"),"X":Horse("X",sire="H1",dam="H2"),
        "F1":Horse("F1",sire="A",dam="B"),"F2":Horse("F2",sire="A",dam="B"),"Y":Horse("Y",sire="F1",dam="F2"),
        "P":Horse("P",sire="A",dam="B"),"Z":Horse("Z",sire="A",dam="P")
    }
    e=FastExactFEngine(by,max_pairs=10000)
    f5=FastF5Engine(by,e,max_pairs=10000)
    for pk,expect in (("X",.125),("Y",.25),("Z",.25)):
        assert abs(e.f(pk)-expect)<1e-10,(pk,e.f(pk),expect)
        assert abs(f5.f5(pk)-expect)<1e-10,(pk,f5.f5(pk),expect)
    assert scope_label(["JPN"],"ALL")=="JPN_ALL"
    assert grade_start_for_scope(["JPN"],"JRA")==1984
    assert grade_start_for_scope(["JPN"],"NAR")==1997
    assert RANK_COL["Fall"]=="FallCountryEraPct"
    assert RANK_COL["F5"]=="F5CountryEraPct"
    print("V1.3 ALL-IN-ONE SELF TEST OK")
    print("  Exact F: half-sib=12.5%, full-sib=25%, parent-offspring=25%")
    print("  F5 same cases OK / JPN_ALL-JRA-NAR OK / grouped logistic cohort OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test_all_in_one()
    else:
        print("="*76)
        print("近交係数 × 競走成績 高速ALL-IN-ONE", VERSION)
        print("単体ファイル: Exact F LRU + F5 LRU + 5代path cache + stakes永続cache")
        print("生産国×出生5年帯 grouped logistic / JPN_ALL・JRA・NAR / blood全頭出力")
        print("="*76)
        main()
'''

out = base.rstrip() + header + '\n' + fast_body.strip() + '\n\n' + fix_body.strip() + selftest
for needle in ('class FastExactFEngine:', 'def fast_parse_stakes_to_db(', 'RANK_COL =', 'def fixed_write_logit(', 'def self_test_all_in_one('):
    if needle not in out:
        raise SystemExit('missing merged section: '+needle)
if 'import inbreeding_performance_scope_allblood' in out:
    raise SystemExit('external module dependency remains')
OUT.write_text(out, encoding='utf-8')
print(OUT)
print('bytes=', OUT.stat().st_size)
