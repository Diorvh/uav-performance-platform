"""Read-only battery and discharge-curve selection API.

The GUI and simulation callers need two different guarantees from the
database.  A catalog caller must be able to see every sample, including a
sample without an imported curve, while a simulation caller must receive a
curve belonging to the explicitly selected sample.  ``BatterySelection``
provides that boundary without importing the GUI or opening a writable
database connection.

Availability is deliberately conservative and local to one test:

* the test must be reviewed (not excluded);
* its stored integration summary must say ``integration_valid``; and
* at least two points must be present.

The module has no dependencies beyond Python's standard library and can also
be used as a small JSON CLI via ``python -m battery_discharge.selection``.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1

# The keys are the point-column names used by ``database.POINT_KEYS``.  Values
# are intentionally short, stable unit strings so that non-Python simulation
# clients can consume the selected curve without importing ``models.py``.
POINT_UNITS = {
    "elapsed_s": "s",
    "voltage_v": "V",
    "current_a": "A",
    "temperature_c": "℃",
    "power_w": "W",
    "capacity_ah": "Ah",
    "energy_wh": "Wh",
    "remaining_pct": "% (relative remaining capacity)",
    "soc_pct": "% (relative SOC)",
}


_REQUIRED_CATALOG_TABLES = {"categories", "samples"}
_TEST_TABLE = "discharge_tests"
_POINT_TABLE = "discharge_points"


def _json_object(value: Any) -> dict[str, Any]:
    """Decode a stored JSON object without allowing malformed metadata to
    break catalog browsing.

    The importer writes JSON objects, but returning an empty object for a
    damaged row lets the selection API still report that row and its
    availability reason.  It also keeps the read-only boundary explicit: no
    repair is attempted here.
    """

    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _integer_id(value: Any) -> int | None:
    """Return a usable positive integer identifier, or ``None``."""

    # The public Python API is deliberately strict.  In particular, accepting
    # ``1.5`` and silently converting it to ``1`` could select another
    # battery; the CLI already supplies native integers through argparse.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    number = value
    return number if number > 0 else None


def _natural_code_key(value: Any) -> tuple[int, Any, str]:
    """Keep numeric category codes in human order while supporting text."""

    text = "" if value is None else str(value)
    if text.isdigit():
        return (0, int(text), text)
    return (1, text.casefold(), text)


class BatterySelection:
    """A context-managed, read-only view of one battery database.

    ``BatterySelection`` opens SQLite in ``mode=ro`` and additionally enables
    SQLite's connection-level ``query_only`` flag.  The class never calls
    schema initialization or migration, so constructing it cannot create or
    modify a database.
    """

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).resolve()
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA query_only=ON")
        self._closed = False
        self._tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    def __enter__(self) -> "BatterySelection":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the read-only connection.  Calling this more than once is safe."""

        if not self._closed:
            self.connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("BatterySelection 已关闭")

    @contextmanager
    def _snapshot(self):
        """Run a group of reads against one SQLite read transaction."""

        self._ensure_open()
        was_in_transaction = self.connection.in_transaction
        if not was_in_transaction:
            self.connection.execute("BEGIN")
        try:
            yield
        finally:
            # Rollback only the transaction started here.  It releases the
            # snapshot without ever attempting a database write.
            if not was_in_transaction and self.connection.in_transaction:
                self.connection.rollback()

    def _has_catalog(self) -> bool:
        return _REQUIRED_CATALOG_TABLES.issubset(self._tables)

    def _sample_rows(self, category_id: int | None = None) -> list[sqlite3.Row]:
        """Read raw sample rows in the same flattened shape as Database.samples."""

        self._ensure_open()
        if not self._has_catalog():
            return []
        where = ""
        params: tuple[Any, ...] = ()
        if category_id is not None:
            where = " WHERE s.category_id=?"
            params = (category_id,)
        return self.connection.execute(
            """
            SELECT s.*, c.form_factor, c.category_code
            FROM samples AS s
            JOIN categories AS c ON c.id=s.category_id
            """
            + where
            + " ORDER BY c.form_factor, c.category_code, s.id",
            params,
        ).fetchall()

    def _curve_rows(self, sample_id: int | None = None) -> list[sqlite3.Row]:
        """Read test rows and point counts without loading point payloads."""

        self._ensure_open()
        if _TEST_TABLE not in self._tables:
            return []
        params: tuple[Any, ...] = ()
        where = ""
        if sample_id is not None:
            where = " WHERE t.sample_id=?"
            params = (sample_id,)
        if _POINT_TABLE in self._tables:
            point_count = (
                "COALESCE((SELECT COUNT(*) FROM discharge_points AS p "
                "WHERE p.test_id=t.id), 0) AS point_count"
            )
        else:
            point_count = "0 AS point_count"
        return self.connection.execute(
            "SELECT t.*, "
            + point_count
            + " FROM discharge_tests AS t"
            + where
            + " ORDER BY t.test_date, t.id",
            params,
        ).fetchall()

    @staticmethod
    def _sample_display_name(
        metadata: dict[str, Any],
        category_code: Any,
        sample_code: Any,
        form_factor: Any = None,
    ) -> str:
        """Use summary codes, with safe identity-aware fallback formatting."""

        summary_code = metadata.get("summary_code")
        if summary_code is not None and str(summary_code).strip():
            return str(summary_code).strip()
        sample = "" if sample_code is None else str(sample_code).strip()
        if sample.startswith("原编号"):
            return sample[len("原编号") :]
        # Pouch sample codes are serial numbers.  Their category_code is a
        # descriptive catalog identity, not a cylinder category prefix.
        if str(form_factor or "") == "软包":
            return sample
        category = "" if category_code is None else str(category_code).strip()
        if category and sample:
            if sample == category or sample.startswith(category + "-"):
                return f"{category}-{sample}" if sample == category else sample
            return f"{category}-{sample}"
        return sample or category

    @staticmethod
    def _sample_from_row(row: sqlite3.Row) -> dict[str, Any]:
        row_values = dict(row)
        item = dict(row_values)
        metadata = _json_object(item.pop("metadata_json", None))
        item.update(metadata)
        # JSON is payload, while SQL columns identify the actual catalog row.
        # Restore every non-JSON SQL value after merging so a malformed or
        # stale metadata object cannot change the selected sample identity.
        for key, value in row_values.items():
            if key != "metadata_json":
                item[key] = value
        item["display_name"] = BatterySelection._sample_display_name(
            metadata,
            row_values.get("category_code"),
            row_values.get("sample_code"),
            row_values.get("form_factor"),
        )
        return item

    @staticmethod
    def _availability(
        row: sqlite3.Row | dict[str, Any],
        summary: dict[str, Any],
        point_count: int,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        review_status = row["review_status"] if isinstance(row, sqlite3.Row) else row.get("review_status")
        if review_status != "reviewed":
            reasons.append("excluded" if review_status == "excluded" else "review_status_not_reviewed")
        if summary.get("integration_valid") is not True:
            reasons.append("integration_invalid")
        if point_count < 2:
            reasons.append("insufficient_points")
        return not reasons, reasons

    @classmethod
    def _curve_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        row_values = dict(row)
        item = dict(row_values)
        metadata = _json_object(item.pop("metadata_json", None))
        summary = _json_object(item.pop("summary_json", None))
        try:
            point_count = int(row_values.get("point_count", 0) or 0)
        except (TypeError, ValueError):
            point_count = 0
        item.update(metadata)
        item.update(summary)
        # Keep SQL identity/review/source columns authoritative after the
        # metadata and summary payloads are flattened into the response.
        for key, value in row_values.items():
            if key not in {"metadata_json", "summary_json", "point_count"}:
                item[key] = value
        available, reasons = cls._availability(row, summary, point_count)
        test_id = item.get("id")
        item["test_id"] = test_id
        item["point_count"] = point_count
        item["available"] = available
        item["unavailable_reason"] = "; ".join(reasons) if reasons else None
        item["unavailable_reasons"] = reasons
        # Nested copies make the selected record self-describing while the
        # flattened fields retain compatibility with Database.tests().
        item["metadata"] = metadata
        item["summary"] = summary
        return item

    def _counts_by_sample(self) -> dict[int, tuple[int, int]]:
        counts: dict[int, list[int]] = {}
        for row in self._curve_rows():
            sample_id = row["sample_id"]
            if sample_id not in counts:
                counts[sample_id] = [0, 0]
            counts[sample_id][0] += 1
            curve = self._curve_from_row(row)
            if curve["available"]:
                counts[sample_id][1] += 1
        return {sample_id: (values[0], values[1]) for sample_id, values in counts.items()}

    def list_categories(self, form_factor: str | None = None) -> list[dict[str, Any]]:
        """List category records, including samples and curve availability."""

        self._ensure_open()
        if "categories" not in self._tables:
            return []
        params: tuple[Any, ...] = ()
        where = ""
        if form_factor not in (None, ""):
            if str(form_factor) in {"其他", "__other__"}:
                # Match the GUI's three-way selector: 半固态、其他、软包
                # are all presented under the user-facing 其他 group.
                where = " WHERE c.form_factor NOT IN ('18650','21700')"
            else:
                where = " WHERE c.form_factor=?"
                params = (form_factor,)
        categories = self.connection.execute(
            "SELECT c.id AS category_id, c.form_factor, c.category_code "
            "FROM categories AS c" + where,
            params,
        ).fetchall()
        samples = self._sample_rows()
        sample_by_category: dict[int, list[dict[str, Any]]] = {}
        for row in samples:
            sample = self._sample_from_row(row)
            sample_by_category.setdefault(sample["category_id"], []).append(sample)
        curve_counts = self._counts_by_sample()

        result: list[dict[str, Any]] = []
        for row in categories:
            category_id = row["category_id"]
            category_samples = sample_by_category.get(category_id, [])
            models = sorted(
                {
                    str(sample.get("model_name")).strip()
                    for sample in category_samples
                    if sample.get("model_name") not in (None, "")
                },
                key=str.casefold,
            )
            category_code = row["category_code"]
            display_name = str(category_code or "")
            if models:
                display_name = f"{display_name}-{' / '.join(models)}"
            curve_count = sum(curve_counts.get(sample["id"], (0, 0))[0] for sample in category_samples)
            available_count = sum(curve_counts.get(sample["id"], (0, 0))[1] for sample in category_samples)
            result.append(
                {
                    "category_id": category_id,
                    "form_factor": row["form_factor"],
                    "category_code": category_code,
                    "display_name": display_name,
                    "sample_count": len(category_samples),
                    "curve_count": curve_count,
                    "available_curve_count": available_count,
                    "simulation_available": available_count > 0,
                }
            )
        return sorted(
            result,
            key=lambda item: (
                str(item.get("form_factor") or ""),
                _natural_code_key(item.get("category_code")),
                item["category_id"],
            ),
        )

    def list_samples(self, category_id: int | None = None) -> list[dict[str, Any]]:
        """List every catalog sample, including samples with no usable curve."""

        self._ensure_open()
        parsed_category_id = None if category_id is None else _integer_id(category_id)
        if category_id is not None and parsed_category_id is None:
            return []
        counts = self._counts_by_sample()
        result: list[dict[str, Any]] = []
        for row in self._sample_rows(parsed_category_id):
            item = self._sample_from_row(row)
            curve_count, available_count = counts.get(item["id"], (0, 0))
            item.update(
                {
                    "curve_count": curve_count,
                    "available_curve_count": available_count,
                    "has_curve": curve_count > 0,
                    "simulation_available": available_count > 0,
                }
            )
            result.append(item)
        return sorted(
            result,
            key=lambda item: (
                str(item.get("form_factor") or ""),
                _natural_code_key(item.get("category_code")),
                item["id"],
            ),
        )

    def list_curves(self, sample_id: int) -> list[dict[str, Any]]:
        """Return every stored test for one exact sample, usable or not."""

        parsed_sample_id = _integer_id(sample_id)
        if parsed_sample_id is None:
            return []
        return [self._curve_from_row(row) for row in self._curve_rows(parsed_sample_id)]

    def _sample(self, sample_id: int) -> dict[str, Any] | None:
        parsed_sample_id = _integer_id(sample_id)
        if parsed_sample_id is None:
            return None
        if not self._has_catalog():
            return None
        row = self.connection.execute(
            """
            SELECT s.*, c.form_factor, c.category_code
            FROM samples AS s
            JOIN categories AS c ON c.id=s.category_id
            WHERE s.id=?
            """,
            (parsed_sample_id,),
        ).fetchone()
        if row is None:
            return None
        sample = self._sample_from_row(row)
        curves = [self._curve_from_row(curve_row) for curve_row in self._curve_rows(parsed_sample_id)]
        curve_count = len(curves)
        available_count = sum(1 for curve in curves if curve["available"])
        sample.update(
            {
                "curve_count": curve_count,
                "available_curve_count": available_count,
                "has_curve": curve_count > 0,
                "simulation_available": available_count > 0,
            }
        )
        return sample

    @staticmethod
    def _response(
        status: str,
        message: str,
        sample: dict[str, Any] | None,
        curves: list[dict[str, Any]],
        selection: dict[str, Any] | None = None,
        curve: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "message": message,
            "sample": sample,
            "curves": curves,
            "selection": selection,
            "curve": curve,
        }

    def _points(self, test_id: int) -> list[dict[str, Any]]:
        if _POINT_TABLE not in self._tables:
            return []
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM discharge_points WHERE test_id=? ORDER BY point_order",
                (test_id,),
            ).fetchall()
        ]

    def _select_curve_in_snapshot(
        self, sample_id: int, test_id: int | None = None
    ) -> dict[str, Any]:
        """Implementation of :meth:`select_curve` inside one read snapshot."""

        parsed_sample_id = _integer_id(sample_id)
        sample = None if parsed_sample_id is None else self._sample(parsed_sample_id)
        if sample is None:
            return self._response(
                "sample_not_found",
                "数据库为空或指定电池不存在",
                None,
                [],
            )

        curves = self.list_curves(parsed_sample_id)
        available = [curve for curve in curves if curve.get("available") is True]

        if test_id is not None:
            parsed_test_id = _integer_id(test_id)
            selected = next(
                (curve for curve in curves if curve.get("id") == parsed_test_id),
                None,
            )
            if parsed_test_id is None or selected is None:
                return self._response(
                    "curve_not_found",
                    "指定曲线不存在或不属于该电池",
                    sample,
                    curves,
                )
            if selected.get("available") is not True:
                reason = selected.get("unavailable_reason") or "unknown"
                return self._response(
                    "curve_unavailable",
                    f"指定曲线不可用于仿真：{reason}",
                    sample,
                    curves,
                )
        elif not available:
            return self._response(
                "no_curve",
                "该电池没有可用于仿真的曲线",
                sample,
                curves,
            )
        elif len(available) > 1:
            return self._response(
                "selection_required",
                "该电池有多条可用曲线，请明确指定 test_id",
                sample,
                curves,
            )
        else:
            selected = available[0]

        # ``selected`` is available by this point, and its id came from the
        # sample-scoped query above.  No preferred/latest fallback is used.
        selected_id = int(selected["id"])
        selected_curve = dict(selected)
        selected_curve["points"] = self._points(selected_id)
        selected_curve["units"] = dict(POINT_UNITS)
        selection = {
            "sample_id": parsed_sample_id,
            "test_id": selected_id,
            "fingerprint": selected.get("fingerprint"),
            "source_hash": selected.get("source_hash"),
        }
        return self._response(
            "ready",
            "已选择可用于仿真的曲线",
            sample,
            curves,
            selection,
            selected_curve,
        )

    def select_curve(self, sample_id: int, test_id: int | None = None) -> dict[str, Any]:
        """Select one simulation curve without guessing across tests or samples."""

        with self._snapshot():
            return self._select_curve_in_snapshot(sample_id, test_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="电池放电数据库只读选择接口")
    parser.add_argument("--database", type=Path, required=True, help="SQLite 数据库路径")
    commands = parser.add_subparsers(dest="command", required=True)

    categories = commands.add_parser("categories", help="列出电池类别")
    categories.add_argument("--form-factor", default=None)

    samples = commands.add_parser("samples", help="列出具体电池")
    samples.add_argument("--category-id", type=int, default=None)

    curves = commands.add_parser("curves", help="列出某一电池的全部测试曲线")
    curves.add_argument("--sample-id", type=int, required=True)

    select = commands.add_parser("select", help="选择某一电池的仿真曲线")
    select.add_argument("--sample-id", type=int, required=True)
    select.add_argument("--test-id", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    """Run the UTF-8 JSON command-line interface."""

    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    with BatterySelection(args.database) as selection:
        if args.command == "categories":
            result: Any = selection.list_categories(args.form_factor)
        elif args.command == "samples":
            result = selection.list_samples(args.category_id)
        elif args.command == "curves":
            result = selection.list_curves(args.sample_id)
        else:
            result = selection.select_curve(args.sample_id, args.test_id)
    # The desktop process may use a locale-specific console encoding.  Force
    # the JSON stream to UTF-8 when the stream supports reconfiguration; this
    # also preserves Chinese metadata when stdout is redirected to a file.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, TypeError):
        pass
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()


__all__ = ["BatterySelection", "POINT_UNITS", "SCHEMA_VERSION", "main"]
