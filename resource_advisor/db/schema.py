import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vm_skus (
    subscription_id TEXT NOT NULL,
    location        TEXT NOT NULL,
    sku_name        TEXT NOT NULL,
    vcpus           INTEGER NOT NULL,
    memory_mb       REAL NOT NULL,
    last_updated    TEXT NOT NULL,
    PRIMARY KEY (subscription_id, location, sku_name)
);

CREATE TABLE IF NOT EXISTS aks_node_skus (
    location        TEXT NOT NULL,
    sku_name        TEXT NOT NULL,
    last_updated    TEXT NOT NULL,
    PRIMARY KEY (location, sku_name)
);

CREATE TABLE IF NOT EXISTS k8s_versions (
    location        TEXT NOT NULL,
    version         TEXT NOT NULL,
    is_preview      INTEGER NOT NULL DEFAULT 0,
    last_updated    TEXT NOT NULL,
    PRIMARY KEY (location, version)
);

CREATE TABLE IF NOT EXISTS sku_pricing (
    location        TEXT NOT NULL,
    sku_name        TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    hourly_price    REAL NOT NULL,
    last_updated    TEXT NOT NULL,
    PRIMARY KEY (location, sku_name, service_name)
);
"""


def init_db(path: str) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn
