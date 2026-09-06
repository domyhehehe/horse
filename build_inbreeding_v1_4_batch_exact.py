#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_3_all_in_one.py'
OUT = ROOT / 'inbreeding_performance_scope_allblood_pyto_v1_4_batch_exact.py'

src = SRC.read_text(encoding='utf-8')
src = src.replace('from collections import defaultdict, Counter', 'from collections import defaultdict, Counter')
if 'from array import array' not in src:
    src = src.replace('from collections import defaultdict, Counter\n', 'from collections import defaultdict, Counter\nfrom array import array\n')
src = re.sub(r'VERSION\s*=\s*"[^"]+"', 'VERSION = "1.4.0-batch-exact-rowL"', src, count=1)

marker = '\ndef self_test_all_in_one():'
if marker not in src:
    raise SystemExit('self-test marker not found')

layer = r'''

# ===== v1.4 batch Exact F core =====
# Henderson A=L D L' の対角だけを、親が子より前になる順で計算する。
# 1頭ごとの kinship(sire,dam) 再帰は使わない。

class BatchPedigreeIndex:
    def __init__(self, by):
        self.by = by
        self._depth = {}
        self._state = {}
        self.cycle_hits = 0
        for pk in by:
            self.depth_of(pk)
        self.order = sorted(
            by,
            key=lambda pk: (
                self._depth.get(pk, 0),
                by[pk].year if by[pk].year is not None else 9999,
                pk,
            ),
        )
        self.idx = {pk:i for i,pk in enumerate(self.order)}
        n = len(self.order)
        self.sire = array('i', [-1]) * n
        self.dam = array('i', [-1]) * n
        self.depth = array('H', [0]) * n
        self.broken_parent_links = 0
        for i,pk in enumerate(self.order):
            h = by[pk]
            self.depth[i] = max(0, min(65535, int(self._depth.get(pk, 0))))
            for which,p in ((0,h.sire),(1,h.dam)):
                j = self.idx.get(p, -1) if p and p != pk else -1
                # 親は必ず子より前。cycle/逆向きリンクは未知親として切る。
                if j >= i:
                    if j >= 0:
                        self.broken_parent_links += 1
                    j = -1
                if which == 0:
                    self.sire[i] = j
                else:
                    self.dam[i] = j
        self.max_depth = max(self.depth) if n else 0

    def depth_of(self, pk):
        if not pk or pk not in self.by:
            return -1
        if pk in self._depth:
            return self._depth[pk]
        st = self._state.get(pk, 0)
        if st == 1:
            self.cycle_hits += 1
            return -1
        self._state[pk] = 1
        h = self.by[pk]
        vals = []
        for p in (h.sire, h.dam):
            if p and p != pk and p in self.by:
                d = self.depth_of(p)
                if d >= 0:
                    vals.append(d)
        z = 1 + max(vals) if vals else 0
        self._depth[pk] = z
        self._state[pk] = 2
        return z

    def pair_key_i(self, i):
        s = self.sire[i]
        d = self.dam[i]
        if s < 0 or d < 0:
            return None
        return (s,d) if s <= d else (d,s)

    def parents_pk(self, pk):
        i = self.idx.get(pk)
        if i is None:
            return '', ''
        s = self.sire[i]
        d = self.dam[i]
        return (
            self.order[s] if s >= 0 else '',
            self.order[d] if d >= 0 else '',
        )


class RowLDiagonalWorker:
    """1つの父母pairについて row L を祖先方向へ1回だけ伝播する。"""
    def __init__(self, ped, D):
        self.ped = ped
        self.D = D
        n = len(ped.order)
        self.coef = array('d', [0.0]) * n
        self.mark = array('I', [0]) * n
        self.stamp = 0
        self.buckets = [[] for _ in range(int(ped.max_depth) + 1)]
        self.row_traces = 0
        self.ancestor_nodes = 0

    def child_f(self, s, d, d_child):
        self.stamp += 1
        if self.stamp >= 0xFFFFFFFE:
            self.mark = array('I', [0]) * len(self.mark)
            self.stamp = 1
        stamp = self.stamp
        coef = self.coef
        mark = self.mark
        buckets = self.buckets
        depth = self.ped.depth
        sire = self.ped.sire
        dam = self.ped.dam
        D = self.D

        # row L の自己成分 L_ii=1 の寄与
        aii = d_child
        maxdep = max(depth[s], depth[d])

        # sire/dam はそれぞれ0.5。same-parentなら合算して1.0。
        if mark[s] != stamp:
            mark[s] = stamp
            coef[s] = 0.5
            buckets[depth[s]].append(s)
        else:
            coef[s] += 0.5
        if mark[d] != stamp:
            mark[d] = stamp
            coef[d] = 0.5
            buckets[depth[d]].append(d)
        else:
            coef[d] += 0.5

        touched = 0
        for dep in range(int(maxdep), -1, -1):
            bucket = buckets[dep]
            # depth(parent) < depth(child) なので同一depth内に依存はない。
            for j in bucket:
                c = coef[j]
                aii += c * c * D[j]
                touched += 1
                half = 0.5 * c
                p = sire[j]
                if p >= 0:
                    if mark[p] != stamp:
                        mark[p] = stamp
                        coef[p] = half
                        buckets[depth[p]].append(p)
                    else:
                        coef[p] += half
                p = dam[j]
                if p >= 0:
                    if mark[p] != stamp:
                        mark[p] = stamp
                        coef[p] = half
                        buckets[depth[p]].append(p)
                    else:
                        coef[p] += half
            bucket.clear()

        self.row_traces += 1
        self.ancestor_nodes += touched
        f = aii - 1.0
        if -1e-11 < f < 0.0:
            f = 0.0
        if 1.0 < f < 1.0 + 1e-11:
            f = 1.0
        return f


class BatchExactCore:
    def __init__(self, by):
        self.by = by
        self.ped = BatchPedigreeIndex(by)

    def compute(self, cached_f=None, progress=False):
        cached_f = cached_f or {}
        ped = self.ped
        n = len(ped.order)
        F = array('d', [0.0]) * n
        D = array('d', [0.0]) * n
        pair_cache = {}
        worker = RowLDiagonalWorker(ped, D)
        cache_hits = 0
        sibling_hits = 0
        t0 = time.time()

        for i,pk in enumerate(ped.order):
            s = ped.sire[i]
            d = ped.dam[i]
            fs = F[s] if s >= 0 else -1.0
            fd = F[d] if d >= 0 else -1.0
            di = 0.5 - 0.25 * (fs + fd)
            D[i] = di

            cv = cached_f.get(pk)
            if cv is not None:
                fi = float(cv)
                cache_hits += 1
            elif s < 0 or d < 0:
                # 未知親は他の全個体と無関係なfounderとして扱うのでF=0。
                fi = 0.0
            else:
                key = (s,d) if s <= d else (d,s)
                hit = pair_cache.get(key)
                if hit is not None:
                    fi = hit
                    sibling_hits += 1
                else:
                    fi = worker.child_f(s, d, di)
                    pair_cache[key] = fi
            F[i] = fi
            if s >= 0 and d >= 0:
                key = (s,d) if s <= d else (d,s)
                pair_cache.setdefault(key, fi)

            if progress and (i+1) % 25000 == 0:
                avg = worker.ancestor_nodes / worker.row_traces if worker.row_traces else 0.0
                print(
                    f"  Fall batch {i+1:,}/{n:,}  {time.time()-t0:.1f}s  "
                    f"row_traces={worker.row_traces:,} sibling_hit={sibling_hits:,} "
                    f"cached={cache_hits:,} avg_anc={avg:.1f}"
                )
        return F, D, worker, pair_cache


class _BatchDepthAdapter:
    def __init__(self, by, ped):
        self.by = by
        self.ped = ped
    def parents(self, pk):
        return self.ped.parents_pk(pk)
    def depth(self, pk):
        i = self.ped.idx.get(pk)
        return int(self.ped.depth[i]) if i is not None else 0


def batch_compute_all_exact(by, con):
    # 既存cacheは数学的に同じExact Fなのでそのまま再利用可能。
    existing = {}
    cached_f = {}
    for row in con.execute(
        "SELECT pk,fall,f5,common_n,path_pair_n,nearest_cross,source_hhi,max_source_share,structure_class FROM exact"
    ):
        existing[row[0]] = row[1:]
        if row[1] is not None:
            cached_f[row[0]] = float(row[1]) / 100.0

    todo = [pk for pk in by if pk not in existing]
    if not todo:
        print("Exact F/F5 cache: 全頭再利用")
        return

    print(f"Exact F/F5/5代構造 新規計算: {len(todo):,}頭")
    print("  v1.4: row-L batch Exact F / sibling pair reuse / F5・構造も父母pair reuse")

    # Phase 1: Fall を全血統順序で一括計算。
    core = BatchExactCore(by)
    if core.ped.broken_parent_links:
        print(f"  注意: cycle/逆向き親リンク {core.ped.broken_parent_links:,}件を未知親扱い")
    F, D, worker, _pairs = core.compute(cached_f=cached_f, progress=True)
    avg = worker.ancestor_nodes / worker.row_traces if worker.row_traces else 0.0
    print(
        f"Fall batch 完了: row traces={worker.row_traces:,}, "
        f"平均distinct ancestors={avg:.1f}"
    )

    # Phase 2: F5と5代構造。全兄弟は同じなので父母pairごとに1回。
    adapter = _BatchDepthAdapter(by, core.ped)
    f5e = FastF5Engine(by, adapter, max_pairs=700_000)
    struct = RecentStructureCache(by)
    f5_pair = {}
    struct_pair = {}

    # 既存cacheから父母pair cacheをseed。
    for pk,vals in existing.items():
        i = core.ped.idx.get(pk)
        if i is None:
            continue
        key = core.ped.pair_key_i(i)
        if key is None:
            continue
        f5v,cn,pn,nc,hhi,mx,cl = vals[1:]
        if f5v is not None:
            f5_pair.setdefault(key, float(f5v))
        if cn is not None:
            struct_pair.setdefault(key, (cn,pn,nc,hhi,mx,cl))

    sql = "INSERT OR REPLACE INTO exact VALUES(?,?,?,?,?,?,?,?,?)"
    batch = []
    t0 = time.time()
    f5_hits = 0
    st_hits = 0
    for n_done,pk in enumerate(todo,1):
        i = core.ped.idx[pk]
        key = core.ped.pair_key_i(i)
        fall = F[i] * 100.0
        if key is None:
            f5 = 0.0
            stv = (0,0,"",None,None,"NO_RECENT_COMMON")
        else:
            if key in f5_pair:
                f5 = f5_pair[key]
                f5_hits += 1
            else:
                f5 = f5e.f5(pk) * 100.0
                f5_pair[key] = f5
            if key in struct_pair:
                stv = struct_pair[key]
                st_hits += 1
            else:
                stv = struct.structure(pk)
                struct_pair[key] = stv
        cn,pn,nc,hhi,mx,cl = stv
        batch.append((pk,fall,f5,cn,pn,nc,hhi,mx,cl))
        if len(batch) >= 2000:
            con.executemany(sql,batch)
            con.commit()
            batch.clear()
        if n_done % 25000 == 0:
            print(
                f"  F5/構造 {n_done:,}/{len(todo):,}  {time.time()-t0:.1f}s  "
                f"pair_hit F5={f5_hits:,} structure={st_hits:,}"
            )
    if batch:
        con.executemany(sql,batch)
        con.commit()
    print("Exact F/F5/5代構造 batch 完了")


# main() が参照するglobalを新コアに置換。
compute_all_exact = batch_compute_all_exact


def self_test_batch_exact():
    # 基本3ケース
    by={
        "A":Horse("A"),"B":Horse("B"),"C":Horse("C"),
        "H1":Horse("H1",sire="A",dam="B"),"H2":Horse("H2",sire="A",dam="C"),"X":Horse("X",sire="H1",dam="H2"),
        "F1":Horse("F1",sire="A",dam="B"),"F2":Horse("F2",sire="A",dam="B"),"Y":Horse("Y",sire="F1",dam="F2"),
        "P":Horse("P",sire="A",dam="B"),"Z":Horse("Z",sire="A",dam="P")
    }
    core=BatchExactCore(by)
    F,_,_,_=core.compute()
    got={pk:F[core.ped.idx[pk]] for pk in by}
    for pk,expect in (("X",.125),("Y",.25),("Z",.25)):
        assert abs(got[pk]-expect)<1e-10,(pk,got[pk],expect)

    # 少し大きい決定論的pedigreeで旧Exact再帰と全頭一致確認。
    by2={f"P{i}":Horse(f"P{i}") for i in range(12)}
    for i in range(12,260):
        s=(i*7+3)%i
        d=(i*11+5)%i
        if d==s:
            d=(d+1)%i
        by2[f"P{i}"]=Horse(f"P{i}",sire=f"P{s}",dam=f"P{d}")
    old=FastExactFEngine(by2,max_pairs=500000)
    c2=BatchExactCore(by2)
    f2,_,_,_=c2.compute()
    for pk in by2:
        a=old.f(pk)
        b=f2[c2.ped.idx[pk]]
        assert abs(a-b)<1e-9,(pk,a,b)
    print("V1.4 BATCH EXACT SELF TEST OK: row-L == recursive Exact F")


def self_test_all_in_one_v14():
    self_test_all_in_one()
    self_test_batch_exact()
'''

src = src.replace(marker, layer + marker, 1)
src = src.replace('self_test_all_in_one()\n    else:', 'self_test_all_in_one_v14()\n    else:', 1)
src = src.replace('高速ALL-IN-ONE', 'BATCH EXACT ALL-IN-ONE')
src = src.replace('Exact F LRU + F5 LRU + 5代path cache + stakes永続cache', 'Exact F row-L batch + sibling pair reuse + F5/5代pair cache + stakes永続cache')

OUT.write_text(src, encoding='utf-8')
print(OUT)
print('bytes=', OUT.stat().st_size)
