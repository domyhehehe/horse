#!/usr/bin/env python3
from pathlib import Path
p=Path('inbreeding_performance_scope_allblood_pyto_v1_4_batch_exact.py')
s=p.read_text(encoding='utf-8')
old='''        for pop,where in (\n            ("ALL_BLOOD","1=1"),\n            ("STAKES_TOP3","ScopeFlatStakesTop3=1"),\n        ):'''
new='''        for pop,where in (\n            ("ALL_BLOOD","AnalysisBirthYearEligible=1"),\n            ("STAKES_TOP3","AnalysisBirthYearEligible=1 AND ScopeFlatStakesTop3=1"),\n        ):'''
if old not in s:
    raise SystemExit('target block not found')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')
print('patched birth-year filter')
