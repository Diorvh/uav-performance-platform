from __future__ import annotations

import sqlite3
from pathlib import Path

from .excel_import import duplicate_candidates
from .models import GroupCandidate


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS test_groups (
    id                     INTEGER PRIMARY KEY,
    display_order          INTEGER NOT NULL UNIQUE,
    motor_spec             TEXT NOT NULL,
    test_date              TEXT,
    propeller              TEXT,
    nominal_voltage_v      REAL,
    esc_advance_deg        REAL,
    pwm_frequency_khz      REAL,
    am32_motor_kv          INTEGER,
    no_load_current_a      REAL,
    source_file            TEXT NOT NULL,
    source_workbook_index  INTEGER NOT NULL,
    source_sheet            TEXT NOT NULL,
    source_sheet_index      INTEGER NOT NULL,
    source_block_index      INTEGER NOT NULL,
    source_anchor           TEXT NOT NULL,
    repeat_set              INTEGER,
    analysis_preferred      INTEGER NOT NULL DEFAULT 0 CHECK (analysis_preferred IN (0, 1)),
    notes                   TEXT
);

CREATE TABLE IF NOT EXISTS measurements (
    id                  INTEGER PRIMARY KEY,
    group_id            INTEGER NOT NULL REFERENCES test_groups(id) ON DELETE CASCADE,
    point_order         INTEGER NOT NULL,
    throttle_us         REAL NOT NULL,
    voltage_v           REAL,
    current_a           REAL,
    rpm                 REAL,
    thrust_mn           REAL,
    power_w             REAL,
    thrust_g            REAL,
    efficiency_g_per_w  REAL,
    source_row          INTEGER,
    UNIQUE (group_id, point_order)
);

"""


def group_name(row: sqlite3.Row) -> str:
    voltage = "?V" if row["nominal_voltage_v"] is None else f'{row["nominal_voltage_v"]:g}V'
    return f'{row["display_order"]:03d}-{row["motor_spec"]}-{row["propeller"] or "桨叶待确认"}-{voltage}'


def shown(value: object) -> str:
    if value is None or value == "":
        return "—"
    return f"{value:g}" if isinstance(value, float) else str(value)


class Database:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(test_groups)")}
        if "test_date" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE test_groups ADD COLUMN test_date TEXT")
        marker_columns_added = False
        if "repeat_set" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE test_groups ADD COLUMN repeat_set INTEGER")
            marker_columns_added = True
        if "analysis_preferred" not in columns:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE test_groups ADD COLUMN analysis_preferred INTEGER NOT NULL DEFAULT 0"
                )
            marker_columns_added = True
        with self.connection:
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_test_groups_repeat_set ON test_groups(repeat_set)")
        self.refresh_repeat_sets(initialize=marker_columns_added)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _repeat_key(row: sqlite3.Row) -> tuple[object, ...]:
        return tuple(
            row[column]
            for column in (
                "motor_spec", "propeller", "nominal_voltage_v", "esc_advance_deg",
                "pwm_frequency_khz", "am32_motor_kv", "no_load_current_a",
            )
        )

    def refresh_repeat_sets(self, initialize: bool = False) -> None:
        rows = self.connection.execute(
            "SELECT id, display_order, motor_spec, propeller, nominal_voltage_v, "
            "esc_advance_deg, pwm_frequency_khz, am32_motor_kv, no_load_current_a, "
            "repeat_set, analysis_preferred FROM test_groups ORDER BY display_order"
        ).fetchall()
        buckets: dict[tuple[object, ...], list[sqlite3.Row]] = {}
        for row in rows:
            buckets.setdefault(self._repeat_key(row), []).append(row)
        used_sets = {row["repeat_set"] for row in rows if row["repeat_set"] is not None}
        next_set = max(used_sets, default=0) + 1
        assignments: dict[int, int | None] = {}
        for bucket in buckets.values():
            if len(bucket) < 2:
                assignments[bucket[0]["id"]] = None
                continue
            existing_sets = {row["repeat_set"] for row in bucket if row["repeat_set"] is not None}
            repeat_set = min(existing_sets) if existing_sets else next_set
            if not existing_sets:
                next_set += 1
            for row in bucket:
                assignments[row["id"]] = repeat_set
        with self.connection:
            for group_id, repeat_set in assignments.items():
                self.connection.execute(
                    "UPDATE test_groups SET repeat_set=? WHERE id=?",
                    (repeat_set, group_id),
                )
            for bucket in buckets.values():
                if initialize and len(bucket) > 1:
                    self.connection.executemany(
                        "UPDATE test_groups SET analysis_preferred=0 WHERE id=?",
                        [(row["id"],) for row in bucket],
                    )

    def set_analysis_preferred(self, group_id: int, preferred: bool = True) -> None:
        row = self.connection.execute(
            "SELECT repeat_set FROM test_groups WHERE id=?", (group_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未知数据组 id={group_id}")
        with self.connection:
            if preferred and row["repeat_set"] is not None:
                self.connection.execute(
                    "UPDATE test_groups SET analysis_preferred=0 WHERE repeat_set=?",
                    (row["repeat_set"],),
                )
            self.connection.execute(
                "UPDATE test_groups SET analysis_preferred=? WHERE id=?",
                (1 if preferred else 0, group_id),
            )

    def analysis_groups(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM test_groups "
            "WHERE repeat_set IS NULL OR analysis_preferred=1 ORDER BY display_order"
        ).fetchall()

    def groups(self, search: str = "") -> list[sqlite3.Row]:
        term = f"%{search.strip()}%"
        return self.connection.execute(
            """
            SELECT * FROM test_groups
            WHERE ? = '%%'
               OR motor_spec LIKE ?
               OR test_date LIKE ?
               OR propeller LIKE ?
               OR CAST(nominal_voltage_v AS TEXT) LIKE ?
               OR CAST(esc_advance_deg AS TEXT) LIKE ?
               OR CAST(pwm_frequency_khz AS TEXT) LIKE ?
               OR CAST(am32_motor_kv AS TEXT) LIKE ?
               OR CAST(no_load_current_a AS TEXT) LIKE ?
            ORDER BY display_order
            """,
            (term,) * 9,
        ).fetchall()

    def points(self, group_ids: list[int]) -> list[sqlite3.Row]:
        if not group_ids:
            return []
        placeholders = ",".join("?" for _ in group_ids)
        return self.connection.execute(
            f"""
            SELECT m.*, g.display_order, g.motor_spec, g.propeller, g.nominal_voltage_v
            FROM measurements AS m
            JOIN test_groups AS g ON g.id = m.group_id
            WHERE m.group_id IN ({placeholders})
            ORDER BY g.display_order, m.point_order
            """,
            group_ids,
        ).fetchall()

    def replace_with_reviewed_groups(self, groups: list[GroupCandidate]) -> None:
        """只接受已经移除重复项的列表，并按保留后的顺序连续编号。"""
        if duplicate_candidates(groups):
            raise ValueError("仍存在重复候选，必须人工审查并移除后才能写入数据库")
        with self.connection:
            self.connection.execute("DELETE FROM measurements")
            self.connection.execute("DELETE FROM test_groups")
            self._insert_groups(groups, 1)
        self.refresh_repeat_sets()

    def append_reviewed_groups(self, groups: list[GroupCandidate], start_order: int | None = None) -> None:
        """追加已经审批的数据组，不删除此前已导入的数据。"""
        if duplicate_candidates(groups):
            raise ValueError("仍存在重复候选，必须人工审查并移除后才能写入数据库")
        with self.connection:
            existing = {
                tuple(row)
                for row in self.connection.execute(
                    "SELECT source_file, source_sheet, source_anchor, nominal_voltage_v FROM test_groups"
                )
            }
            incoming = {
                (group.source_file, group.source_sheet, group.source_anchor, group.nominal_voltage_v)
                for group in groups
            }
            if existing & incoming:
                raise ValueError("待导入数据已存在于数据库，已停止以避免重复录入")
            if start_order is None:
                start_order = self.connection.execute(
                    "SELECT COALESCE(MAX(display_order), 0) + 1 FROM test_groups"
                ).fetchone()[0]
            self._insert_groups(groups, start_order)
        self.refresh_repeat_sets()

    def _insert_groups(self, groups: list[GroupCandidate], start_order: int) -> None:
        for display_order, group in enumerate(groups, start_order):
            group_id = self.connection.execute(
                """
                INSERT INTO test_groups (
                    display_order, motor_spec, test_date, propeller, nominal_voltage_v,
                    esc_advance_deg, pwm_frequency_khz, am32_motor_kv,
                    no_load_current_a, source_file, source_workbook_index,
                    source_sheet, source_sheet_index, source_block_index, source_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    display_order,
                    group.motor_spec,
                    group.test_date,
                    group.propeller,
                    group.nominal_voltage_v,
                    group.esc_advance_deg,
                    group.pwm_frequency_khz,
                    group.am32_motor_kv,
                    group.no_load_current_a,
                    group.source_file,
                    group.source_workbook_index,
                    group.source_sheet,
                    group.source_sheet_index,
                    group.source_block_index,
                    group.source_anchor,
                ),
            ).lastrowid
            self.connection.executemany(
                """
                INSERT INTO measurements (
                    group_id, point_order, throttle_us, voltage_v, current_a,
                    rpm, thrust_mn, power_w, thrust_g, efficiency_g_per_w, source_row
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        group_id,
                        point.point_order,
                        point.throttle_us,
                        point.voltage_v,
                        point.current_a,
                        point.rpm,
                        point.thrust_mn,
                        point.power_w,
                        point.thrust_g,
                        point.efficiency_g_per_w,
                        point.source_row,
                    )
                    for point in group.points
                ],
            )
