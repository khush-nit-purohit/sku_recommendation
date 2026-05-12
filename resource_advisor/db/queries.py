import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_stale(conn: sqlite3.Connection, subscription_id: str, location: str, ttl_days: int = 7) -> bool:
    row = conn.execute(
        "SELECT min(last_updated) FROM vm_skus WHERE subscription_id = ? AND location = ?",
        (subscription_id, location.lower()),
    ).fetchone()
    if not row or not row[0]:
        return True
    oldest = datetime.fromisoformat(row[0])
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - oldest) > timedelta(days=ttl_days)


def get_vm_skus(conn: sqlite3.Connection, subscription_id: str, location: str) -> list[dict]:
    rows = conn.execute(
        "SELECT sku_name, vcpus, memory_mb FROM vm_skus WHERE subscription_id = ? AND location = ?",
        (subscription_id, location.lower()),
    ).fetchall()
    return [dict(r) for r in rows]


def get_aks_skus(conn: sqlite3.Connection, location: str) -> set[str]:
    rows = conn.execute(
        "SELECT sku_name FROM aks_node_skus WHERE location = ?",
        (location.lower(),),
    ).fetchall()
    return {r["sku_name"].lower() for r in rows}


def get_k8s_versions(conn: sqlite3.Connection, location: str) -> list[str]:
    rows = conn.execute(
        "SELECT version FROM k8s_versions WHERE location = ? AND is_preview = 0",
        (location.lower(),),
    ).fetchall()
    return [r["version"] for r in rows]


def get_price(conn: sqlite3.Connection, location: str, sku_name: str, service_name: str) -> Optional[float]:
    row = conn.execute(
        "SELECT hourly_price FROM sku_pricing WHERE location = ? AND sku_name = ? AND service_name = ?",
        (location.lower(), sku_name, service_name),
    ).fetchone()
    return float(row["hourly_price"]) if row else None


def upsert_vm_skus(conn: sqlite3.Connection, subscription_id: str, location: str, skus: list[dict]):
    now = _now_iso()
    conn.executemany(
        """INSERT INTO vm_skus (subscription_id, location, sku_name, vcpus, memory_mb, last_updated)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(subscription_id, location, sku_name) DO UPDATE SET
               vcpus=excluded.vcpus, memory_mb=excluded.memory_mb, last_updated=excluded.last_updated""",
        [(subscription_id, location.lower(), s["name"], s["vcpus"], s["memory_mb"], now) for s in skus],
    )
    conn.commit()


def upsert_aks_skus(conn: sqlite3.Connection, location: str, sku_names: list[str]):
    now = _now_iso()
    conn.executemany(
        """INSERT INTO aks_node_skus (location, sku_name, last_updated)
           VALUES (?, ?, ?)
           ON CONFLICT(location, sku_name) DO UPDATE SET last_updated=excluded.last_updated""",
        [(location.lower(), name, now) for name in sku_names],
    )
    conn.commit()


def upsert_k8s_versions(conn: sqlite3.Connection, location: str, versions: list[dict]):
    now = _now_iso()
    conn.executemany(
        """INSERT INTO k8s_versions (location, version, is_preview, last_updated)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(location, version) DO UPDATE SET
               is_preview=excluded.is_preview, last_updated=excluded.last_updated""",
        [(location.lower(), v["version"], 1 if v.get("is_preview") else 0, now) for v in versions],
    )
    conn.commit()


def upsert_price(conn: sqlite3.Connection, location: str, sku_name: str, service_name: str, hourly_price: float):
    now = _now_iso()
    conn.execute(
        """INSERT INTO sku_pricing (location, sku_name, service_name, hourly_price, last_updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(location, sku_name, service_name) DO UPDATE SET
               hourly_price=excluded.hourly_price, last_updated=excluded.last_updated""",
        (location.lower(), sku_name, service_name, hourly_price, now),
    )
    conn.commit()
