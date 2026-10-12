"""Calculations for raw battery-discharge samples.

The importer deliberately keeps the instrument's raw time, voltage, and
current columns.  This module derives capacity and energy from those columns
using the trapezoid rule.  It does not repair, sort, de-duplicate, or bridge
over a bad sample; callers can therefore review the original row that caused
an invalid integration.
"""

from __future__ import annotations

from datetime import date, datetime, time
import math
from numbers import Real
from typing import Any, Iterable

from .models import FORMULA_VERSION


def _finite_number(value: Any) -> float | None:
    """Return a finite float, or ``None`` for missing/non-numeric values."""

    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _raw_time_seconds(value: Any) -> float | None:
    """Best-effort conversion used when the public function is called directly.

    ``excel_import`` performs the stricter unit and Excel-date handling.  This
    fallback makes ``calculate_points`` useful with already-normalized Python
    data while still refusing an unlabelled numeric time value.
    """

    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, time.min).timestamp()
    if isinstance(value, time):
        return (
            value.hour * 3600
            + value.minute * 60
            + value.second
            + value.microsecond / 1_000_000
        )
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # The importer normally converts this first.  The common explicit
        # elapsed forms are safe to accept here as well.
        import re

        match = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(ms|milliseconds?|毫秒|s|sec(?:onds?)?|秒|m|min(?:utes?)?|分钟|h|hr|hours?|小时)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            number = float(match.group(1))
            unit = match.group(2).lower()
            if unit in {"ms", "millisecond", "milliseconds", "毫秒"}:
                return number / 1000.0
            if unit in {"s", "sec", "second", "seconds", "秒"}:
                return number
            if unit in {"m", "min", "minute", "minutes", "分钟"}:
                return number * 60.0
            return number * 3600.0
        # ISO/datalog timestamps.  Underscore is the format used by DL24.
        try:
            return datetime.fromisoformat(text.replace("_", " ")).timestamp()
        except ValueError:
            return None
    return None


def _source_label(point: dict[str, Any], index: int) -> str:
    row = point.get("source_row")
    return f"源行 {row}" if row is not None else f"采样点 {index + 1}"


def _append_issue(issues: list[str], message: str) -> None:
    if message not in issues:
        issues.append(message)


def calculate_points(
    points: Iterable[dict[str, Any]],
    initial_soc_pct: float | None = None,
    reference_capacity_ah: float | None = None,
    soc_basis: str | None = None,
    max_gap_s: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Calculate cumulative capacity/energy for normalized raw points.

    The input points are copied, preserving all existing raw fields.  Adjacent
    rows are integrated only when both rows contain a finite timestamp,
    voltage, and non-negative current and the interval is strictly positive.
    A zero or negative interval, a configured over-large gap, or a malformed
    sample is reported and never silently bridged.  The returned summary keeps
    the cumulative values reached before/after invalid intervals as provisional
    values while setting ``integration_valid`` to ``False``.

    ``remaining_pct`` is relative to this run's integrated total.  It and
    ``soc_pct`` are emitted only when the entire integration is valid.  SOC is
    intentionally conservative: a positive reference capacity *and* a
    non-empty explicit ``soc_basis`` are required in addition to
    ``initial_soc_pct``.
    """

    out = [dict(point) for point in points]
    issues: list[str] = []
    critical = False

    if max_gap_s is not None:
        gap_limit = _finite_number(max_gap_s)
        if gap_limit is None or gap_limit <= 0:
            _append_issue(issues, "采样间隔阈值必须为有限正数")
            critical = True
            gap_limit = None
    else:
        gap_limit = None

    # Normalize derived fields while retaining the raw fields supplied by the
    # importer.  If elapsed_s is missing, only explicitly parseable datetime
    # strings are accepted here; unlabelled numerics remain invalid.
    for index, point in enumerate(out):
        elapsed = _finite_number(point.get("elapsed_s"))
        if elapsed is None and point.get("elapsed_s") is None:
            raw_seconds = _raw_time_seconds(point.get("raw_time"))
            if raw_seconds is not None:
                elapsed = raw_seconds
        point["elapsed_s"] = elapsed

        voltage = _finite_number(point.get("voltage_v"))
        current = _finite_number(point.get("current_a"))
        point["voltage_v"] = voltage
        point["current_a"] = current

        if voltage is None:
            _append_issue(issues, f"电压缺失或非有限数值：{_source_label(point, index)}")
            critical = True
        elif voltage <= 0:
            _append_issue(issues, f'电压非正值：{_source_label(point, index)}')
            critical = True
        if current is None:
            _append_issue(issues, f"电流缺失或非有限数值：{_source_label(point, index)}")
            critical = True
        elif current < 0:
            _append_issue(issues, f"负电流，需确认充放电阶段：{_source_label(point, index)}，{current:g} A")
            critical = True
        if elapsed is None:
            _append_issue(issues, f"时间缺失或无效：{_source_label(point, index)}")
            critical = True

        point["power_w"] = voltage * current if voltage is not None and current is not None else None
        if point['power_w'] is not None and not math.isfinite(point['power_w']):
            point['power_w'] = None
            _append_issue(issues, f'功率数值溢出：{_source_label(point, index)}')
            critical = True
        point["capacity_ah"] = None
        point["energy_wh"] = None
        point["remaining_pct"] = None
        point["soc_pct"] = None

    # Convert absolute timestamps used by direct callers into elapsed seconds
    # relative to the first valid point.  Importer-created elapsed_s values are
    # already relative and start at zero; the conversion is harmless for them.
    valid_elapsed = [point["elapsed_s"] for point in out if point.get("elapsed_s") is not None]
    if valid_elapsed and out and any(
        point.get("elapsed_s") is not None and abs(point["elapsed_s"]) > 10_000_000
        for point in out
    ):
        base = valid_elapsed[0]
        for point in out:
            if point.get("elapsed_s") is not None:
                point["elapsed_s"] = point["elapsed_s"] - base

    cumulative_ah = 0.0
    cumulative_wh = 0.0
    have_any_valid_point = False

    for index, point in enumerate(out):
        current_valid = (
            point.get("elapsed_s") is not None
            and point.get("voltage_v") is not None
            and point.get("current_a") is not None
            and point.get("current_a") >= 0
        )

        # A point records the cumulative value available immediately before
        # any interval ending at it.  Keeping these values on invalid rows lets
        # the GUI display the last safe provisional total without inventing a
        # bridge over the row.
        point["capacity_ah"] = cumulative_ah if have_any_valid_point or current_valid else None
        point["energy_wh"] = cumulative_wh if have_any_valid_point or current_valid else None

        if not current_valid:
            continue

        if not have_any_valid_point:
            have_any_valid_point = True
            point["capacity_ah"] = cumulative_ah
            point["energy_wh"] = cumulative_wh
            continue

        previous = out[index - 1] if index > 0 else None
        previous_valid = bool(
            previous
            and previous.get("elapsed_s") is not None
            and previous.get("voltage_v") is not None
            and previous.get("current_a") is not None
            and previous.get("current_a") >= 0
        )
        if not previous_valid:
            # Do not connect this point to an older valid point.  The next
            # interval may resume from this point if it is itself valid.
            point["capacity_ah"] = cumulative_ah
            point["energy_wh"] = cumulative_wh
            continue

        dt = point["elapsed_s"] - previous["elapsed_s"]
        if not math.isfinite(dt):
            _append_issue(issues, f"invalid time interval at {_source_label(previous, index - 1)} -> {_source_label(point, index)}")
            critical = True
        elif dt <= 0:
            _append_issue(
                issues,
                f"时间未递增：{_source_label(previous, index - 1)} → {_source_label(point, index)}，间隔 {dt:g} s",
            )
            if dt == 0 and (
                previous.get("voltage_v") != point.get("voltage_v")
                or previous.get("current_a") != point.get("current_a")
            ):
                _append_issue(
                    issues,
                    f"重复时间戳对应的测量值冲突：{_source_label(previous, index - 1)} → {_source_label(point, index)}",
                )
            critical = True
        elif gap_limit is not None and dt > gap_limit:
            _append_issue(
                issues,
                f"采样间隔超过审查阈值，未跨越该间隔积分：{_source_label(previous, index - 1)} → {_source_label(point, index)}，{dt:g} s",
            )
            critical = True
        else:
            previous_power = previous.get("power_w")
            current_power = point.get("power_w")
            if previous_power is None or current_power is None:
                _append_issue(
                    issues,
                    f"cannot integrate non-finite power at {_source_label(previous, index - 1)} -> {_source_label(point, index)}",
                )
                critical = True
            else:
                cumulative_ah += 0.5 * (previous["current_a"] + point["current_a"]) * dt / 3600.0
                cumulative_wh += 0.5 * (previous_power + current_power) * dt / 3600.0

        point["capacity_ah"] = cumulative_ah
        point["energy_wh"] = cumulative_wh

    # With no usable samples, retain a clear zero/empty summary rather than
    # presenting a fabricated capacity.  A zero total cannot define relative
    # remaining capacity, so it is invalid for this workflow.
    finite_voltage = [point["voltage_v"] for point in out if point.get("voltage_v") is not None]
    duration_s: float | None = None
    elapsed_values = [point["elapsed_s"] for point in out if point.get("elapsed_s") is not None]
    if elapsed_values:
        candidate_duration = elapsed_values[-1] - elapsed_values[0]
        if math.isfinite(candidate_duration) and candidate_duration >= 0:
            duration_s = candidate_duration
        else:
            _append_issue(issues, "timestamps are not ordered for a valid duration")
            critical = True

    total_capacity = cumulative_ah if have_any_valid_point else None
    total_energy = cumulative_wh if have_any_valid_point else None
    if total_capacity is None or total_capacity <= 0:
        _append_issue(issues, "积分总容量不是正数，不能计算相对 SOC")
        critical = True

    if not math.isfinite(cumulative_ah) or not math.isfinite(cumulative_wh):
        _append_issue(issues, '容量或能量积分溢出')
        critical = True
        total_capacity = total_energy = None
    integration_valid = bool(out) and not critical
    mean_voltage = total_energy / total_capacity if total_capacity and total_energy is not None else None

    # Relative remaining capacity and SOC are deliberately gated on a clean
    # integration.  This prevents a chart from looking authoritative after a
    # duplicate timestamp or a gap has invalidated the run.
    if integration_valid and total_capacity is not None and total_capacity > 0:
        for point in out:
            capacity = point.get("capacity_ah")
            if capacity is not None:
                point["remaining_pct"] = 100.0 * (1.0 - capacity / total_capacity)

        initial = _finite_number(initial_soc_pct)
        reference = _finite_number(reference_capacity_ah)
        basis = str(soc_basis).strip() if soc_basis is not None else ""
        if initial is not None and reference is not None and reference > 0 and basis:
            for point in out:
                capacity = point.get("capacity_ah")
                if capacity is not None:
                    point["soc_pct"] = initial - 100.0 * capacity / reference

    summary = {
        "total_capacity_ah": total_capacity,
        "total_energy_wh": total_energy,
        "duration_s": duration_s,
        "mean_voltage_v": mean_voltage,
        "integration_valid": integration_valid,
        "formula_version": FORMULA_VERSION,
    }
    return out, summary, issues


__all__ = ["calculate_points"]
