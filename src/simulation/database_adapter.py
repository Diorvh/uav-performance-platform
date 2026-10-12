"""力效 SQLite 的独立只读适配层，不导入数据库项目的可写 Database 类。

读取短事务快照 → 检查人工采用状态 → 用户选择同配置实验组 → 统一电功率/推力口径。
不迁移、不更新标记、不拟合、不平均重复实验，也不默默丢弃坏点或单点电压层。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from platform_config import database_id

from .propulsion_model import PropulsionMap, PropulsionPoint
from .fitted_propulsion import FittedPropulsionMap


GROUP_COLUMNS = (
    "id", "display_order", "motor_spec", "propeller", "test_date", "nominal_voltage_v",
    "esc_advance_deg", "pwm_frequency_khz", "am32_motor_kv", "no_load_current_a",
    "source_file", "source_workbook_index", "source_sheet", "source_sheet_index",
    "source_block_index", "source_anchor", "repeat_set", "analysis_preferred", "notes",
)
MEASUREMENT_COLUMNS = (
    "id", "group_id", "point_order", "source_row", "throttle_us", "voltage_v", "current_a",
    "rpm", "thrust_mn", "thrust_g", "power_w", "efficiency_g_per_w",
)
HARDWARE_COLUMNS = ("motor_spec", "propeller", "esc_advance_deg", "pwm_frequency_khz", "am32_motor_kv")


@dataclass(frozen=True)
class DatabaseSnapshot:
    """一个关闭连接后的逻辑快照；sha256 标识所读表内容，不冒充数据库文件哈希。"""

    path: str
    read_at_utc: str
    user_version: int
    groups: tuple[dict, ...]
    measurements: tuple[dict, ...]
    repeat_adopted_counts: dict[int, int]
    sha256: str

    def status(self, group: dict) -> str:
        """重复集合必须恰好一条采用记录；冲突只阻止受影响组。"""
        flag, repeat = group["analysis_preferred"], group["repeat_set"]
        if flag not in (0, 1):
            return "标记非法"
        if repeat is None:
            return "可采用"
        members = [g for g in self.groups if g["repeat_set"] == repeat]
        if any(g["analysis_preferred"] not in (0, 1) for g in members):
            return "标记非法"
        count = self.repeat_adopted_counts[repeat]
        if count > 1:
            return "采用冲突"
        if count == 0:
            return "待人工采用"
        return "可采用" if flag == 1 else "未采用"

    @property
    def eligible_groups(self) -> tuple[dict, ...]:
        return tuple(g for g in self.groups if self.status(g) == "可采用")

    def points_for(self, group_id: int) -> tuple[dict, ...]:
        """用真实关联键查询，绝不把显示序号当 group_id。"""
        return tuple(p for p in self.measurements if p["group_id"] == group_id)


@dataclass(frozen=True)
class DatabasePropulsion:
    """可用于飞行计算的推进表，以及可导出重现的来源/原始输入记录。"""

    propulsion_map: PropulsionMap
    provenance: dict


def read_database(path: str | Path) -> DatabaseSnapshot:
    """显式只读 URI，短事务使采用检查和测量数据属于同一次一致性读取。"""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在，不会创建空库：{source}")
    try:
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0.3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            for table, required in (("test_groups", GROUP_COLUMNS), ("measurements", MEASUREMENT_COLUMNS)):
                actual = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                missing = set(required) - actual
                if missing:
                    raise ValueError(f"数据库 {table} 缺少字段：{', '.join(sorted(missing))}；请数据库端检查版本。")
            # 列名来自本模块常量，没有拼接任何用户输入；按名称而非列位置读取。
            groups = tuple(dict(row) for row in connection.execute(
                f"SELECT {', '.join(GROUP_COLUMNS)} FROM test_groups ORDER BY display_order"))
            points = tuple(dict(row) for row in connection.execute(
                f"SELECT {', '.join(MEASUREMENT_COLUMNS)} FROM measurements ORDER BY group_id, point_order"))
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError(f"无法只读访问数据库：{exc}。若正在写入，请待数据库端保存后手动重新读取。") from exc
    ids = {g["id"] for g in groups}
    if any(p["group_id"] not in ids for p in points):
        raise ValueError("数据库存在没有对应实验组的工况点，请数据库端检查外键关系。")
    counts = {g["repeat_set"]: 0 for g in groups if g["repeat_set"] is not None}
    for group in groups:
        if group["repeat_set"] is not None and group["analysis_preferred"] == 1:
            counts[group["repeat_set"]] += 1
    # 保留普通列原值用于审计。非有限值可读取，但选中建模时必须严格拒绝。
    digest = hashlib.sha256(json.dumps([version, groups, points], sort_keys=True,
                                       ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return DatabaseSnapshot(str(source), datetime.now(timezone.utc).isoformat(), version, groups, points, counts, digest)


def build_propulsion(snapshot: DatabaseSnapshot, group_ids: list[int] | tuple[int, ...], *,
                     model_kind: str = "interpolation", allow_extrapolation: bool = False) -> DatabasePropulsion:
    """只接受用户明确选择的可采用组；单点层、同电压多实验、混合硬件均拒绝。

    动力支路按 V*(I-I0) 建净功率表；整机用电器功率在仿真器中手动另加。
    从原始 V/I/I0 重算，与库内扣空载口径一致，避免使用可能过期的派生列。
    """
    if model_kind not in ("interpolation", "shared_fit"):
        raise ValueError("未知推进模型。")
    if allow_extrapolation and model_kind != "shared_fit":
        raise ValueError("只有共享指数拟合模型支持电压外推。")
    if not group_ids or len(set(group_ids)) != len(group_ids):
        raise ValueError("请选择不重复的实验组；Ctrl/Shift 可以多选不同电压层。")
    by_id = {g["id"]: g for g in snapshot.groups}
    if any(i not in by_id for i in group_ids):
        raise ValueError("实验组关联键已不存在，请重新读取数据库。")
    groups = sorted((by_id[i] for i in group_ids), key=lambda g: g["display_order"])
    for group in groups:
        if snapshot.status(group) != "可采用":
            raise ValueError(f"{group['display_order']:03d} 组：{snapshot.status(group)}。请在数据库端确认采用；仿真器不修改标记。")
    hardware = {tuple(g[k] for k in HARDWARE_COLUMNS) for g in groups}
    if len(hardware) != 1:
        raise ValueError("不能混用不同电机、桨叶、进角、PWM 或 AM32 设置；空值也不能视作等于已知设置。")
    if not groups[0]["motor_spec"] or not groups[0]["propeller"]:
        raise ValueError("选定实验缺少电机或桨叶名称，不能确认推进系统身份。")
    converted, records, voltage_owners = [], [], {}
    warnings = ["动力净功率采用 V×(I−I0)；用电器功率手动另加，不自动补回台架空载基线。请确认实飞仍存在的耗电没有漏计。",
                "数据库推进表仅在实测插值支持内使用，不外推；台架插值不等于实飞最大推力认证。"]
    if any(groups[0][k] is None for k in HARDWARE_COLUMNS[2:]):
        warnings.append("部分电调设置未记录；相同空值仅表示共同缺失，不证明实际硬件配置一致。")
    for group in groups:
        raw_points = snapshot.points_for(group["id"])
        label = f"{group['display_order']:03d}"
        idle_current = group["no_load_current_a"]
        if not isinstance(idle_current, (int, float)) or not math.isfinite(idle_current) or idle_current < 0:
            raise ValueError(f"{label} 组的 no_load_current_a 必须是有限非负数，不能在缺失时默认扣 0。")
        if len(raw_points) < 2:
            raise ValueError(f"{label} 组仅 {len(raw_points)} 个点：已完整读取，但不足以建立电压层曲线。请取消选择该组或补测，不会自动删除或外推。")
        group_voltages = set()
        for raw in raw_points:
            for name in ("voltage_v", "current_a", "rpm", "thrust_mn"):
                value = raw[name]
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{label} 组 measurement_id={raw['id']} 的 {name} 必须是有限正数。")
            voltage = raw["voltage_v"]
            group_voltages.add(voltage)
            thrust = raw["thrust_mn"] / 9.80665  # mN → 标准 gf；不用历史 9.8 换算列。
            net_current = raw["current_a"] - idle_current
            if net_current <= 0:
                raise ValueError(f"{label} 组 measurement_id={raw['id']} 扣除空载后电流不为正，不能用于悬停动力曲线。")
            power = voltage * net_current
            converted.append(PropulsionPoint(voltage, raw["rpm"], thrust, power, thrust / power))
            records.append({"raw": raw, "thrust_n": raw["thrust_mn"] / 1000,
                            "thrust_standard_gf": thrust, "current_propulsion_net_a": net_current,
                            "power_propulsion_net_w": power, "power_measured_total_w": voltage * raw["current_a"],
                            "efficiency_net_gf_per_w": thrust / power})
        if len(group_voltages) != 1:
            raise ValueError(f"{label} 组包含多个实测电压；请数据库端核对分组，本适配器不会按标称电压覆盖实测电压。")
        voltage = next(iter(group_voltages))
        if voltage in voltage_owners:
            raise ValueError(f"{voltage:g} V 同时选择了 {voltage_owners[voltage]} / {label} 两组。即使未标成重复也不能自动拼接，请明确选择一组。")
        voltage_owners[voltage] = label
    try:
        data = PropulsionMap(converted, allow_single_voltage_approximation=False, warnings=tuple(warnings))
        if model_kind == "shared_fit":
            data = FittedPropulsionMap(data, allow_extrapolation=allow_extrapolation)
            data.warnings += tuple(w for w in warnings if w.startswith("部分电调"))
    except ValueError as exc:
        labels = ",".join(format(g["display_order"], "03d") for g in groups)
        raise ValueError(f"所选组 {labels} 不能构建推进表：{exc}") from exc
    provenance = {
        "source_type": "SQLite read-only", "database_path": snapshot.path,
        "database_id": database_id("force", snapshot.path),
        "read_at_utc": snapshot.read_at_utc, "user_version": snapshot.user_version,
        "logical_snapshot_sha256": snapshot.sha256,
        "model": "piecewise linear P(V,thrust) and rpm(V,thrust); no extrapolation",
        "power_basis": "propulsion net: voltage_v * (current_a - no_load_current_a)",
        "avionics_basis": "manual whole-aircraft constant power; no automatic V*I0 add-back",
        "thrust_basis": "thrust_mn / 9.80665 = standard gf",
        "selected_groups": groups, "measurements": records, "warnings": warnings,
    }
    if isinstance(data, FittedPropulsionMap):
        provenance["model"] = "shared voltage exponent; selected hardware log-linear calibration"
        provenance["fitted_model"] = data.metadata()
        provenance["warnings"] = list(data.warnings)
    return DatabasePropulsion(data, provenance)
