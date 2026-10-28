"""Multi-level BOM traversal (where-used). Cycle-safe, depth-bounded, quantity-aware."""
from __future__ import annotations

from ..db import Conn
from ..errors import ToolError

MAX_PATHS = 5000


def get_component(conn: Conn, component_id: str) -> dict:
    row = conn.query_one(
        "SELECT component_id, description, kind, lifecycle_status, criticality, uom FROM components WHERE component_id = ?",
        (component_id,))
    if row is None:
        raise ToolError("not_found", f"unknown component_id {component_id}")
    return row


def where_used(conn: Conn, component_id: str, depth_limit: int = 8) -> dict:
    comp = get_component(conn, component_id)
    parents_cache: dict[str, list[tuple[str, int]]] = {}

    def parents(node: str) -> list[tuple[str, int]]:
        if node not in parents_cache:
            rows = conn.query("SELECT parent_id, qty_per FROM bom_links WHERE child_id = ? ORDER BY parent_id", (node,))
            parents_cache[node] = [(r["parent_id"], int(r["qty_per"])) for r in rows]
        return parents_cache[node]

    affected: dict[str, dict] = {}
    roots: dict[str, dict] = {}
    issues: dict[tuple, dict] = {}
    truncated = False
    path_count = 0
    stack: list[tuple[str, list[str], int]] = [(component_id, [component_id], 1)]
    while stack:
        node, path, qty = stack.pop()
        for parent, q in parents(node):
            if parent in path:
                key = ("cycle_detected", parent)
                issues[key] = {"code": "cycle_detected", "detail": f"BOM cycle through {parent}: {' <- '.join(path + [parent])}"}
                continue
            level = len(path)
            if level > depth_limit:
                truncated = True
                continue
            path_count += 1
            if path_count > MAX_PATHS:
                truncated = True
                stack.clear()
                break
            new_path, new_qty = path + [parent], qty * q
            info = affected.setdefault(parent, {"id": parent, "min_level": level, "paths": 0})
            info["min_level"] = min(info["min_level"], level)
            info["paths"] += 1
            if not parents(parent):
                r = roots.setdefault(parent, {"product_id": parent, "qty_per_unit": 0, "path_count": 0,
                                              "example_path": list(reversed(new_path))})
                r["qty_per_unit"] += new_qty
                r["path_count"] += 1
            stack.append((parent, new_path, new_qty))
    kinds: dict[str, dict] = {}
    for pid in affected:
        c = conn.query_one(
            "SELECT description, kind FROM components WHERE component_id = ?",
            (pid,),
        )
        if c is None:
            raise ToolError(
                "data_integrity",
                f"BOM references missing component {pid}",
            )
        kinds[pid] = c
    affected_list = []
    for pid, info in sorted(affected.items()):
        affected_list.append({"id": pid, "kind": kinds[pid]["kind"], "description": kinds[pid]["description"],
                              "min_level": info["min_level"], "paths": info["paths"]})
    top = []
    for pid, r in sorted(roots.items()):
        r = dict(r)
        r["description"] = kinds[pid]["description"]
        r["kind"] = kinds[pid]["kind"]
        top.append(r)
    return {
        "component_id": component_id,
        "component": {"description": comp["description"], "kind": comp["kind"],
                      "lifecycle_status": comp["lifecycle_status"]},
        "depth_limit": depth_limit,
        "affected": affected_list,
        "affected_ids": sorted(affected),
        "counts": {"assemblies": sum(1 for a in affected_list if a["kind"] == "assembly"),
                   "products": sum(1 for a in affected_list if a["kind"] == "product"),
                   "total": len(affected_list)},
        "top_level": top,
        "issues": sorted(issues.values(), key=lambda i: i["detail"]),
        "truncated": truncated,
    }
