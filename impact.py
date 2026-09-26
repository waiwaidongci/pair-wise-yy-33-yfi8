"""影响范围推算：只按 assets 的线路父子关系推导受影响对象。

与告警台账、页面交互分处维护：本模块不写任何表、不感知通知状态。
"""
from __future__ import annotations

import sqlite3


def child_map(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """返回 parent_id -> [子资产] 的映射。"""
    children: dict[int, list[int]] = {}
    for row in conn.execute("SELECT id,parent_id FROM assets"):
        children.setdefault(row["id"], [])
        if row["parent_id"] is not None:
            children.setdefault(row["parent_id"], []).append(row["id"])
    return children


def affected_asset_ids(conn: sqlite3.Connection, root_asset_id: int) -> list[int]:
    """从故障资产出发，沿父子关系向下收集（含自身）。

    上游（父）线路停运时，其下游全部子线路及挂接用户都视为受影响；
    用 visited 集合防止脏数据成环导致死循环。
    """
    children = child_map(conn)
    seen: set[int] = set()
    stack = [int(root_asset_id)]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, []))
    return sorted(seen)


def affected_facilities(conn: sqlite3.Connection, root_asset_id: int, include_disconnected: bool = False) -> list[dict]:
    """受影响资产子树上挂接的重要用户，按优先级（一级在前）排序。"""
    ids = affected_asset_ids(conn, root_asset_id)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    where = f"asset_id IN ({placeholders})"
    if not include_disconnected:
        where += " AND connected=1"
    rows = conn.execute(
        f"SELECT * FROM facilities WHERE {where} ORDER BY priority,id", ids
    ).fetchall()
    return [dict(row) for row in rows]
