import logging
import sqlite3
from pathlib import Path
from typing import Any

from src.common.error_tracker import handle_fatal_db_error

logger = logging.getLogger(__name__)


class WorkQueue:
    """Generic SQLite-backed work queue with crash-resilient processing.

    Each stage creates a WorkQueue with its own schema. Work items are
    processed and moved to the results table atomically. If the process
    crashes, remaining work items are picked up on the next run.
    """

    def __init__(
        self,
        db_path: Path,
        work_schema: dict[str, str],
        results_schema: dict[str, str],
    ):
        self.db_path = db_path
        self.work_schema = work_schema
        self.results_schema = results_schema

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        work_cols = ", ".join(f"{col} {typ}" for col, typ in self.work_schema.items())
        results_cols = ", ".join(
            f"{col} {typ}" for col, typ in self.results_schema.items()
        )
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS work ({work_cols})")
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS results ({results_cols})")
        self._conn.commit()

    def populate(self, items: list[dict[str, Any]]) -> None:
        """Bulk insert work items. Idempotent: skips if work or results exist."""
        work_count = self._count("work")
        results_count = self._count("results")
        if work_count > 0 or results_count > 0:
            logger.info(
                "Resuming: %d work items remaining, %d results completed",
                work_count,
                results_count,
            )
            return

        if not items:
            return

        columns = list(self.work_schema.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        try:
            self._conn.executemany(
                f"INSERT OR IGNORE INTO work ({col_names}) VALUES ({placeholders})",
                [tuple(item[col] for col in columns) for item in items],
            )
            self._conn.commit()
            logger.info("Populated work queue with %d items", len(items))
        except sqlite3.Error as e:
            handle_fatal_db_error(e)

    def fetch_batch(self, batch_size: int) -> list[dict[str, Any]]:
        """Fetch next batch of work items."""
        columns = ", ".join(self.work_schema.keys())
        cursor = self._conn.execute(
            f"SELECT {columns} FROM work LIMIT ?", (batch_size,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def fetch_random(self, n: int) -> list[dict[str, Any]]:
        """Fetch n random work items."""
        columns = ", ".join(self.work_schema.keys())
        cursor = self._conn.execute(
            f"SELECT {columns} FROM work ORDER BY RANDOM() LIMIT ?", (n,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def complete(
        self, work_id_column: str, work_id_value: Any, result: dict[str, Any]
    ) -> None:
        """Atomically write result and remove work item."""
        result_columns = list(self.results_schema.keys())
        placeholders = ", ".join("?" for _ in result_columns)
        col_names = ", ".join(result_columns)
        try:
            with self._conn:
                self._conn.execute(
                    f"INSERT INTO results ({col_names}) VALUES ({placeholders})",
                    tuple(result[col] for col in result_columns),
                )
                self._conn.execute(
                    f"DELETE FROM work WHERE {work_id_column} = ?",
                    (work_id_value,),
                )
        except sqlite3.Error as e:
            handle_fatal_db_error(e)

    def remove_batch(self, id_column: str, id_values: list[Any]) -> None:
        """Remove multiple work items by ID."""
        if not id_values:
            return
        placeholders = ", ".join("?" for _ in id_values)
        try:
            self._conn.execute(
                f"DELETE FROM work WHERE {id_column} IN ({placeholders})",
                id_values,
            )
            self._conn.commit()
        except sqlite3.Error as e:
            handle_fatal_db_error(e)

    def is_complete(self) -> bool:
        """Check if all work has been processed."""
        return self._count("work") == 0

    def work_remaining(self) -> int:
        """Count of items left in work table."""
        return self._count("work")

    def results_count(self) -> int:
        """Count of completed items in results table."""
        return self._count("results")

    def fetch_all_results(self) -> list[dict[str, Any]]:
        """Fetch all rows from results table."""
        columns = ", ".join(self.results_schema.keys())
        cursor = self._conn.execute(f"SELECT {columns} FROM results")
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def _count(self, table: str) -> int:
        cursor = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]
