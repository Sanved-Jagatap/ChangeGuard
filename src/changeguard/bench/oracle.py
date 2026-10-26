"""Ground-truth oracle. Deliberately a SEPARATE, naive implementation over plain Python data (no SQL, no
shared code with the tools) so benchmark labels are not produced by the system under test."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from ..seed import SeedData

BLOCKING = {"not_declared_alternate", "supplier_inactive", "lead_time_exceeds_constraint", "compliance_failed"}


def _parents(seed: SeedData):
    par = defaultdict(list)
    for b in seed.bom:
        par[b["child_id"]].append((b["parent_id"], b["qty_per"]))
    return par


TRUE_DEPTH = 64  # ground truth is NOT limited by the tool's default depth limit


def where_used(seed: SeedData, cid: str, depth_limit: int = TRUE_DEPTH):
    par = _parents(seed)
    ids: set[str] = set()
    roots: dict[str, int] = defaultdict(int)
    cycle = False

    def walk(node: str, path: tuple, qty: int, depth: int):
        nonlocal cycle
        for p, q in par[node]:
            if p in path:
                cycle = True
                continue
            if depth > depth_limit:
                continue
            ids.add(p)
            if not par[p]:
                roots[p] += qty * q
            walk(p, path + (p,), qty * q, depth + 1)

    walk(cid, (cid,), 1, 1)
    return ids, dict(roots), cycle


def figures(seed: SeedData, cid: str, site: str | None = None) -> dict:
    ids, roots, _ = where_used(seed, cid)
    units, unplanned = 0, []
    for root, qty in roots.items():
        rows = [d for d in seed.demand if d["product_id"] == root and (site is None or d["site_id"] == site)]
        if not rows:
            unplanned.append(root)
            continue
        units += sum(d["planned_units"] for d in rows) * qty
    exposure = None if (roots and len(unplanned) == len(roots)) else units
    inv = [r for r in seed.inventory if r["component_id"] == cid and (site is None or r["site_id"] == site)]
    avail = None if not inv else sum(max(0, r["on_hand"] - r["reserved"]) for r in inv)
    shortage = None if (exposure is None or avail is None) else max(0, exposure - avail)
    return {"affected_ids": sorted(ids), "affected_count": len(ids), "exposure_units": exposure,
            "available_units": avail, "shortage_units": shortage, "unplanned": sorted(unplanned)}


def evaluate(seed: SeedData, cid: str, cand: str, market: str | None, max_lead: int | None, as_of: str):
    reasons: set[str] = set()
    if (cid, cand) not in set(seed.alternates):
        reasons.add("not_declared_alternate")
    sup = {s["supplier_id"]: s for s in seed.suppliers}
    links = [r for r in seed.component_suppliers if r["component_id"] == cand]
    best = None
    if not links:
        reasons.add("no_supplier_records")
    else:
        active = [r for r in links if sup[r["supplier_id"]]["status"] == "active"]
        if not active:
            reasons.add("supplier_inactive")
        else:
            known = [r["lead_time_days"] for r in active if r["lead_time_days"] is not None]
            if not known:
                reasons.add("missing_lead_time")
            else:
                best = min(known)
                if max_lead is not None and best > max_lead:
                    reasons.add("lead_time_exceeds_constraint")
    reqs = [r for r in seed.requirements if market is None or r["market"] == market]
    if not reqs:
        reasons.add("compliance_not_checked")
    ref = date.fromisoformat(as_of)
    for rq in reqs:
        recs = [r for r in seed.records if r["component_id"] == cand and r["requirement_id"] == rq["requirement_id"]]
        if not recs:
            reasons.add("compliance_not_checked")
            continue
        st = {r["status"] for r in recs}
        if st == {"failed"}:
            reasons.add("compliance_failed")
        elif st == {"confirmed"}:
            dates = [r["evidence_date"] for r in recs if r["evidence_date"]]
            if not dates or (ref - date.fromisoformat(max(dates))).days > 730:
                reasons.add("stale_evidence")
        elif "failed" in st and "confirmed" in st:
            reasons.add("data_conflict")
        else:
            reasons.add("compliance_unresolved")
    if reasons & BLOCKING:
        elig = "blocked"
    elif reasons:
        elig = "insufficient_evidence"
    else:
        elig = "eligible_for_review"
    return elig, sorted(reasons), best


def analyse(seed: SeedData, cid: str, market: str | None, max_lead: int | None, site: str | None, as_of: str) -> dict:
    fig = figures(seed, cid, site)
    cands = [a[1] for a in seed.alternates if a[0] == cid]
    ev = {c: evaluate(seed, cid, c, market, max_lead, as_of) for c in cands}
    eligible = {c: v[2] for c, v in ev.items() if v[0] == "eligible_for_review"}
    return {"figures": fig, "candidates": {c: {"eligibility": v[0], "reasons": v[1], "lead": v[2]} for c, v in ev.items()},
            "eligible": sorted(eligible), "ineligible": sorted(c for c in cands if c not in eligible)}
