"""幂等 seed：只在空库或样例版本变化时写入；重复启动不产生脏数据。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import leases as leases_mod
from . import readings as readings_mod
from . import settings, store


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def load_sample_files(
    readings_path: Path | None = None,
    changes_path: Path | None = None,
    meters_path: Path | None = None,
    leases_path: Path | None = None,
    handovers_path: Path | None = None,
) -> dict:
    readings_path = Path(readings_path or settings.READINGS_SAMPLE)
    changes_path = Path(changes_path or settings.METER_CHANGES_SAMPLE)
    meters_path = Path(meters_path or settings.METERS_SAMPLE)
    leases_path = Path(leases_path or settings.LEASES_SAMPLE)
    handovers_path = Path(handovers_path or settings.HANDOVERS_SAMPLE)
    parsed = readings_mod.parse_csv(readings_path.read_text(encoding="utf-8"))
    changes = json.loads(changes_path.read_text(encoding="utf-8")).get("changes", [])
    meters = json.loads(meters_path.read_text(encoding="utf-8")).get("meters", [])
    leases = leases_mod.parse_leases(
        json.loads(leases_path.read_text(encoding="utf-8")).get("leases", [])
    )
    handovers = leases_mod.parse_handovers(
        json.loads(handovers_path.read_text(encoding="utf-8")).get("handovers", [])
    )
    return {
        "rows": parsed["rows"],
        "stats": parsed["stats"],
        "changes": changes,
        "meters": meters,
        "leases": leases,
        "handovers": handovers,
    }


def seed(
    conn: sqlite3.Connection,
    sample: dict | None = None,
    seed_version: str | None = None,
    force: bool = False,
) -> dict:
    store.init_schema(conn)
    sample = sample or load_sample_files()
    version = seed_version or settings.SEED_VERSION
    current_version = store.get_meta(conn, "seed_version")
    current_rows = store.count_readings(conn)
    if not force and current_version == version and current_rows == len(sample["rows"]) and current_rows > 0:
        return {
            "seeded": False,
            "reason": "已是最新样例版本，跳过（幂等）",
            "seed_version": version,
            "rows": current_rows,
        }

    now = now_iso()
    store.replace_readings(conn, sample["rows"], now)
    store.replace_changes(conn, sample["changes"])
    store.upsert_meters(conn, sample["meters"])
    store.replace_leases(conn, sample["leases"])
    store.replace_handovers(conn, sample["handovers"])
    store.set_meta(conn, "seed_version", version)
    store.set_meta(conn, "seeded_at", now)
    store.add_event(
        conn,
        "seed",
        f"导入样例读数 {len(sample['rows'])} 行 / {len(sample['stats']['meters'])} 块表 / "
        f"{len(sample['changes'])} 条换表 / {len(sample['leases'])} 条租约 / {len(sample['handovers'])} 条交接读数",
        {"seed_version": version, "rows": len(sample["rows"]), "stats": sample["stats"]},
        now,
    )
    conn.commit()
    return {
        "seeded": True,
        "reason": "样例已按当前版本重新导入（DELETE+INSERT 单事务，行数不变）" if force else "样例数据已写入",
        "seed_version": version,
        "rows": len(sample["rows"]),
        "stats": sample["stats"],
        "leases": len(sample["leases"]),
        "handovers": len(sample["handovers"]),
    }
