"""数据边界：CSV 单位转换、逐行校验和原子导出；不承担飞行算法。"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, fields
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from platform_config import software_identity

from .battery_model import CurvePoint, DischargeCurve
from .propulsion_model import PropulsionMap, PropulsionPoint

if TYPE_CHECKING:
    from .simulation import SimulationResult


def _normalize(name: str) -> str:
    """只归一化大小写、全角和分隔符，不用模糊子串猜单位。"""
    return re.sub(r"[\s_()/\[\]-]", "", unicodedata.normalize("NFKC", name).lower())


def _read_csv_rows(path: str | Path) -> list[tuple[int, list[str]]]:
    """兼容 UTF-8/GBK 和三种分隔符；保留物理行号以定位问题。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = Path(path).read_text(encoding="gbk")
    if not text.strip():
        raise ValueError("CSV 文件为空。")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",\t;").delimiter
    except csv.Error:
        first = next(line for line in text.splitlines() if line.strip())
        delimiter = max((",", "\t", ";"), key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    rows = []
    try:
        for row in reader:
            if any(cell.strip() for cell in row):
                rows.append((reader.line_num, [cell.strip() for cell in row]))
    except csv.Error as exc:
        raise ValueError(f"CSV 第 {reader.line_num} 行格式错误：{exc}") from exc
    if not rows:
        raise ValueError("CSV 文件无数据。")
    width = len(rows[0][1])
    for line, row in rows[1:]:
        if len(row) != width:
            raise ValueError(f"CSV 第 {line} 行有 {len(row)} 列，应为 {width} 列；不会跳过坏行。")
    return rows


def _column(header: list[str], aliases: dict[str, float], label: str) -> tuple[int, float] | None:
    """返回列号和该列的单位倍率；重复的同义列必须由用户消歧。"""
    found = [(i, aliases[_normalize(name)]) for i, name in enumerate(header)
             if _normalize(name) in aliases]
    if len(found) > 1:
        raise ValueError(f"{label}有多个同义列，请只保留一个。")
    return found[0] if found else None


def _number(value: str, line: int, label: str) -> float:
    """严格数值转换；非法文本、空值和非有限数不会被当作缺失测量。"""
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"CSV 第 {line} 行的{label}不是数字：{value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"CSV 第 {line} 行的{label}不能是 NaN 或无穷大。")
    return number


def _value(row: list[str], column: tuple[int, float] | None, line: int,
           label: str, optional: bool = False) -> float | None:
    """按本列单位读取；仅 optional 字段允许留空。"""
    if column is None or (optional and not row[column[0]]):
        return None
    value = _number(row[column[0]], line, label) * column[1]
    if not math.isfinite(value):
        raise ValueError(f"CSV 第 {line} 行的{label}单位换算后溢出。")
    return value


VOLTAGE = {"voltagev": 1, "voltage": 1, "电压": 1, "电压v": 1,
           "voltagemv": 0.001, "电压mv": 0.001}
TIME = {"times": 1, "time": 1, "时间": 1, "时间s": 1, "时间秒": 1,
        "timemin": 60, "时间min": 60, "时间分钟": 60,
        "timems": 0.001, "时间ms": 0.001, "时间毫秒": 0.001,
        "timeh": 3600, "时间h": 3600, "时间小时": 3600}


def load_curve_csv(path: str | Path, reference_current_a: float) -> DischargeCurve:
    """恒流整包测试：时间→秒、电压→V；无表头时严格要求两列 秒/V。"""
    rows = _read_csv_rows(path)
    header = rows[0][1]
    time = _column(header, TIME, "时间")
    voltage = _column(header, VOLTAGE, "电压")
    if time is None and voltage is None:
        if len(header) != 2:
            raise ValueError("未识别电池表头；无表头文件必须恰好两列：秒、V。")
        for cell in header:
            _number(cell, rows[0][0], "无表头首行")
        time, voltage = (0, 1), (1, 1)
        data = rows
    elif time is None or voltage is None:
        raise ValueError("电池 CSV 需同时提供受支持的时间和电压列，例如 time_s,voltage_V。")
    else:
        data = rows[1:]
    points = [CurvePoint(_value(row, time, line, "时间"), _value(row, voltage, line, "电压"))
              for line, row in data]
    return DischargeCurve(points, reference_current_a)


def load_propulsion_csv(path: str | Path, default_voltage_v: float,
                        power_basis: str = "net", drive_efficiency: float = 1.0,
                        no_load_current_a: float | None = None) -> PropulsionMap:
    """统一返回扣空载后的单旋翼净功率表。

    net 表已扣空载；electrical/shaft 表须提供 I0（逐行列优先于手动后备值）。
    shaft 先除驱动效率再扣 V*I0。力效和功率的输入口径须一致，容许 3% 舍入差。
    """
    if power_basis not in ("net", "electrical", "shaft"):
        raise ValueError("功率口径必须为 net（已扣空载）、electrical（未扣空载）或 shaft（轴功率）。")
    if not math.isfinite(drive_efficiency) or not 0 < drive_efficiency <= 1:
        raise ValueError("电调×电机总效率必须在 (0,1] 内。")
    if power_basis != "shaft" and drive_efficiency != 1:
        raise ValueError("电输入功率已包含驱动损耗，不能重复扣除电机/电调效率。")
    if no_load_current_a is not None and (not math.isfinite(no_load_current_a) or no_load_current_a < 0):
        raise ValueError("空载电流后备值必须为有限非负数；留空表示未知，不会假定为零。")
    if power_basis == "net" and no_load_current_a not in (None, 0):
        raise ValueError("净功率表已扣空载，请不要再传入非零扣除值。")
    rows = _read_csv_rows(path)
    header = rows[0][1]
    rpm_col = _column(header, {"rpm": 1, "转速": 1, "转速rpm": 1, "转速rmin": 1}, "转速")
    voltage_col = _column(header, VOLTAGE, "电压")
    idle_col = _column(header, {"noloadcurrenta": 1, "空载电流a": 1, "空载电流": 1}, "空载电流")
    gram_col = _column(header, dict.fromkeys(("thrustg", "thrustgf", "推力g", "拉力g", "重量g", "重量"), 1), "克力")
    newton_col = _column(header, dict.fromkeys(("thrustn", "推力n", "拉力n", "牛顿"), 1 / 0.00980665), "牛顿推力")
    power_col = _column(header, {"power": 1, "powerw": 1, "功率": 1, "功率w": 1,
                                "powerkw": 1000, "功率kw": 1000}, "功率")
    efficiency_col = _column(header, dict.fromkeys(("efficiency", "efficiencygperw", "efficiencygw",
                                                   "力效", "力效gw", "gw"), 1), "力效")
    if rpm_col is None or (gram_col is None and newton_col is None):
        raise ValueError("推进表需 rpm 和 thrust_g 或 thrust_N 列（也支持对应中文表头）。")
    if power_col is None and efficiency_col is None:
        raise ValueError("推进表需 power_W 或 efficiency_g_per_W 列；不接受未知功率单位。")
    if voltage_col is None and (not math.isfinite(default_voltage_v) or default_voltage_v <= 0):
        raise ValueError("缺少电压列时，必须填写有效的单电压测试电压。")
    points = []
    for line, row in rows[1:]:
        rpm = _value(row, rpm_col, line, "转速")
        voltage = _value(row, voltage_col, line, "电压") if voltage_col else default_voltage_v
        gram = _value(row, gram_col, line, "克力", optional=True)
        newton = _value(row, newton_col, line, "牛顿推力", optional=True)
        power = _value(row, power_col, line, "功率", optional=True)
        efficiency = _value(row, efficiency_col, line, "力效", optional=True)
        if any(x is not None and x <= 0 for x in (rpm, voltage, gram, newton, power, efficiency)):
            raise ValueError(f"CSV 第 {line} 行的测量量必须为正数；不可用的功率/力效请留空而非填 0。")
        if gram is not None and newton is not None and not math.isclose(gram, newton, rel_tol=0.03):
            raise ValueError(f"CSV 第 {line} 行的 g 与 N 推力不一致（容许 3% 舍入差）。")
        thrust = gram if gram is not None else newton
        if thrust is None or (power is None and efficiency is None):
            raise ValueError(f"CSV 第 {line} 行缺少推力或功率/力效。")
        if power is not None and efficiency is not None and not math.isclose(thrust / power, efficiency, rel_tol=0.03):
            raise ValueError(f"CSV 第 {line} 行的推力、功率和力效不一致（容许 3% 舍入差）。")
        measured_power = power if power is not None else thrust / efficiency
        electrical_power = measured_power / drive_efficiency if power_basis == "shaft" else measured_power
        net_power = electrical_power
        if power_basis != "net":
            idle = _value(row, idle_col, line, "空载电流") if idle_col is not None else no_load_current_a
            if idle is None or not math.isfinite(idle) or idle < 0:
                raise ValueError(f"CSV 第 {line} 行需有限非负的空载电流；请补 no_load_current_A 列或手动后备值。")
            net_power -= voltage * idle
        if not math.isfinite(net_power) or net_power <= 0:
            raise ValueError(f"CSV 第 {line} 行扣空载后的动力净功率必须为正数。")
        points.append(PropulsionPoint(voltage, rpm, thrust, net_power, thrust / net_power))
    return PropulsionMap(points, warnings=("动力采用扣空载净功率，用电器手动另加；请确认未漏计实飞仍存在的耗电。",))


def _output_target(path: str | Path, protected_paths: tuple[str | Path, ...]) -> Path:
    """保护原始输入，包括 SQLite 数据库和等价路径/硬链接。"""
    target = Path(path).resolve()
    for source in protected_paths:
        source = Path(source).resolve()
        if target == source or (target.exists() and source.exists() and target.samefile(source)):
            raise ValueError("结果不能覆盖本次导入的原始数据，请选择另一个文件名。")
    return target


@contextmanager
def _atomic_text(target: Path):
    """原子文本写入的公共实现；失败时不截断已有文件。"""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8-sig",
                                         dir=target.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            yield handle
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_result_csv(result: SimulationResult, path: str | Path,
                    protected_paths: tuple[str | Path, ...] = ()) -> None:
    """完整精度时序；末行与汇总使用同一状态。"""
    with _atomic_text(_output_target(path, protected_paths)) as handle:
        names = [field.name for field in fields(result.points[0])]
        writer = csv.writer(handle)
        writer.writerow(names)
        writer.writerows([getattr(point, name) for name in names] for point in result.points)


def save_run_record(result, params, curve, propulsion_map, path: str | Path,
                    provenance: dict | None = None, protected_paths: tuple[str | Path, ...] = ()) -> None:
    """另存可追溯的 JSON：参数、完整模型输入、来源和汇总，不重复存大时序。

    数据库输入包含 group_id、显示序号、measurement_id、源工作表/行等定位信息。
    """
    record = {
        "record_format_version": 3,
        "software": software_identity(),
        "simulation_model": "quasi-steady hover; incremental loaded battery curve; Euler integration",
        "params": asdict(params),
        "battery": {"reference_current_a": curve.reference_current_a,
                    "points": [asdict(p) for p in curve.points]},
        "propulsion": {"power_basis": "net after no-load subtraction", "thrust_unit": "standard gf",
                       "single_voltage_approximation": propulsion_map.allow_single_voltage_approximation,
                       "points": [asdict(p) for p in propulsion_map.points]},
        "power_accounting": {"total": "propulsion_net + manual_avionics",
                             "avionics_power_w": params.avionics_power_w,
                             "automatic_no_load_add_back": False},
        "provenance": provenance or {},
        "summary": {field.name: getattr(result, field.name) for field in fields(result) if field.name != "points"},
    }
    from .fitted_propulsion import FittedPropulsionMap
    if isinstance(propulsion_map, FittedPropulsionMap):
        record["propulsion"]["fitted_model"] = propulsion_map.metadata()
    record["summary"]["extrapolated_sample_count"] = sum(p.model_extrapolated for p in result.points)
    record["summary"]["rpm_is_estimate"] = any(p.rpm_is_estimate for p in result.points)
    with _atomic_text(_output_target(path, protected_paths)) as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, allow_nan=False)
