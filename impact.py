"""影响范围推算：按线路父子关系，从事故影响区域推出受影响的重要用户。

只负责"谁受影响"，不涉及通知生命周期（见 alarms.py）与页面展示。
"""


class ImpactAnalyzer:
    def __init__(self, conn):
        self.conn = conn

    def affected_assets(self, regions: set[str]) -> list[dict]:
        """命中区域的资产及其全部下游子线路（沿 parent_id 逐级展开）。"""
        rows = self.conn.execute("SELECT * FROM assets ORDER BY id").fetchall()
        children: dict[int, list] = {}
        for row in rows:
            children.setdefault(row["parent_id"], []).append(row)
        found, seen, stack = [], set(), [row for row in rows if row["region"] in regions]
        while stack:
            asset = stack.pop()
            if asset["id"] in seen:
                continue
            seen.add(asset["id"])
            found.append(dict(asset))
            stack.extend(children.get(asset["id"], []))
        return found

    def affected_facilities(self, regions) -> list[dict]:
        """受影响资产上挂载的重要用户，附带所属线路信息。"""
        assets = self.affected_assets(set(regions))
        if not assets:
            return []
        by_id = {asset["id"]: asset for asset in assets}
        marks = ",".join("?" for _ in assets)
        rows = self.conn.execute(
            f"SELECT * FROM facilities WHERE connected=1 AND asset_id IN ({marks}) ORDER BY priority,id",
            [asset["id"] for asset in assets]).fetchall()
        return [{"facility": dict(row), "asset": by_id[row["asset_id"]]} for row in rows]
