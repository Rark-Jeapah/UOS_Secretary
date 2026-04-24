from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from sidae_secretary.db import Database, MIGRATIONS


def _init_worker(db_path: str, queue: mp.Queue) -> None:
    try:
        Database(Path(db_path)).init()
        queue.put({"ok": True})
    except Exception as exc:  # pragma: no cover - assertion uses payload
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def test_migration_init_is_process_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "locked-init.db"
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()

    procs = [ctx.Process(target=_init_worker, args=(str(db_path), queue)) for _ in range(2)]
    for proc in procs:
        proc.start()

    results = [queue.get(timeout=20) for _ in procs]

    for proc in procs:
        proc.join(timeout=20)
        assert proc.exitcode == 0

    assert all(item.get("ok") for item in results), results

    db = Database(db_path)
    db.init()
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT version, COUNT(*) AS c
            FROM schema_migrations
            GROUP BY version
            ORDER BY version
            """
        ).fetchall()

    expected_versions = [version for version, _ in MIGRATIONS]
    assert [int(row["version"]) for row in rows] == expected_versions
    assert all(int(row["c"]) == 1 for row in rows)
