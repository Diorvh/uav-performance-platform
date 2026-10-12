"""悬停飞行的时间步进仿真。

每个时间步的计算顺序：

    总质量 → 所需总推力 → 单桨推力 → 力效地图 → 推进功率
    → 电池电流 → 电池端电压 → 更新已消耗容量

电压和推进工作点相互影响；插值表解二次方程，幂律拟合模型用凸性约束二分。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, fields
from typing import Callable

from .battery_model import DischargeCurve
from .fitted_propulsion import FittedPropulsionMap
# 保持旧脚本的导出入口兼容；文件写入职责已移交 io_utils。
from .io_utils import save_result_csv
from .propulsion_model import (
    MapLimitError,
    PropulsionMap,
    PropulsionOperatingPoint,
)


@dataclass(frozen=True)
class SimulationParams:
    """一次悬停仿真的输入参数。"""

    airframe_mass_g: float
    battery_mass_g: float
    payload_mass_g: float
    rotor_count: int
    thrust_to_weight: float
    avionics_power_w: float  # 整机图传/飞控等用电器的手动恒功率，不乘旋翼数量。
    equivalent_resistance_mohm: float
    cutoff_voltage_v: float
    initial_soc: float
    time_step_s: float
    max_time_s: float
    current_limit_a: float = 0.0

    @property
    def total_mass_g(self) -> float:
        """返回参与悬停的总质量。"""

        return self.airframe_mass_g + self.battery_mass_g + self.payload_mass_g

    @property
    def total_thrust_g(self) -> float:
        """返回总需求推力；纯悬停时推力/重量系数通常为 1。"""

        return self.total_mass_g * self.thrust_to_weight

    @property
    def thrust_per_rotor_g(self) -> float:
        """返回均匀分配到每个旋翼的推力。"""

        return self.total_thrust_g / self.rotor_count


@dataclass(frozen=True)
class SimulationPoint:
    """仿真时序中的一个时间点。"""

    time_s: float
    consumed_ah: float
    soc: float
    baseline_voltage_v: float
    voltage_v: float
    current_a: float
    total_thrust_g: float
    thrust_per_rotor_g: float
    rpm: float
    efficiency_g_per_w: float
    propulsion_power_w: float  # 所有旋翼合计的扣空载动力净功率。
    total_power_w: float
    avionics_power_w: float = 0.0
    propulsion_current_a: float = 0.0  # 所有旋翼净电流，不是单电机电流。
    avionics_current_a: float = 0.0
    model_extrapolated: bool = False  # 超出所选实测表的插值支持。
    rpm_is_estimate: bool = False  # 幂律模型的转速来自独立 T=k*n² 校准。


@dataclass(frozen=True)
class SimulationResult:
    """仿真结果和常用汇总指标。"""

    points: tuple[SimulationPoint, ...]
    duration_s: float
    cutoff_reason: str
    capacity_ah: float
    consumed_ah: float
    end_soc: float
    end_voltage_v: float
    average_current_a: float
    peak_current_a: float
    average_power_w: float
    start_rpm: float
    end_rpm: float
    start_propulsion_power_w: float
    end_propulsion_power_w: float
    total_mass_g: float
    total_thrust_g: float
    used_energy_wh: float = 0.0
    min_current_a: float = 0.0
    termination_kind: str = "limit"
    warnings: tuple[str, ...] = ()
    propulsion_energy_wh: float = 0.0
    avionics_energy_wh: float = 0.0
    avionics_power_w: float = 0.0


class PowerLimitError(ValueError):
    """电池增量模型无法支持所需功率；不是求解器返回一个伪电压。"""


def _solve_flight_point(
    baseline_voltage_v: float,
    curve: DischargeCurve,
    propulsion_map: PropulsionMap,
    params: SimulationParams,
    resistance_ohm: float,
) -> tuple[float, float, PropulsionOperatingPoint, float, float]:
    """插值表解 V²-(E-R*a)V+R*b=0；拟合表调用其高压分支求解器。

    分段内取高电压根，并回代检查电压方程残差；这不是动态稳定性仿真。
    E 只是增量模型的代数量，不应解释为测得的 OCV。
    """
    thrust = params.thrust_per_rotor_g
    effective_source = baseline_voltage_v + resistance_ohm * curve.reference_current_a
    candidates: list[float] = []
    if resistance_ohm == 0:
        candidates.append(baseline_voltage_v)
    elif isinstance(propulsion_map, FittedPropulsionMap):
        try:
            candidates.append(propulsion_map.solve_voltage(
                baseline_voltage_v, curve.reference_current_a, resistance_ohm,
                thrust, params.rotor_count, params.avionics_power_w))
        except MapLimitError:
            raise
        except ValueError as exc:
            raise PowerLimitError(str(exc)) from exc
    elif propulsion_map.is_single_voltage:
        power = (propulsion_map.operate(propulsion_map.voltages[0], thrust).power_w
                 * params.rotor_count + params.avionics_power_w)
        discriminant = effective_source**2 - 4 * resistance_ohm * power
        if discriminant < 0:
            raise PowerLimitError("电池增量模型无可用功率解。")
        candidates.append((effective_source + math.sqrt(discriminant)) / 2)
    else:
        # 每个有效区间内 P(V) 都是直线；覆盖不了目标推力的区间不外推。
        for low, high in propulsion_map.voltage_segments(thrust):
            p_low = propulsion_map.operate(low, thrust).power_w * params.rotor_count + params.avionics_power_w
            p_high = propulsion_map.operate(high, thrust).power_w * params.rotor_count + params.avionics_power_w
            slope = (p_high - p_low) / (high - low)
            intercept = p_low - slope * low
            coefficient = effective_source - resistance_ohm * slope
            discriminant = coefficient**2 - 4 * resistance_ohm * intercept
            if discriminant < 0:
                continue
            voltage = (coefficient + math.sqrt(discriminant)) / 2
            if low - 1e-9 <= voltage <= high + 1e-9:
                candidates.append(min(high, max(low, voltage)))
        # 孤立的可用电压层也可能恰好满足方程。
        for voltage in propulsion_map.voltages:
            try:
                point = propulsion_map.operate(voltage, thrust)
            except MapLimitError:
                continue
            power = point.power_w * params.rotor_count + params.avionics_power_w
            if abs(voltage - effective_source + resistance_ohm * power / voltage) <= 1e-9:
                candidates.append(voltage)
        if not candidates:
            raise MapLimitError("当前力效数据覆盖范围内无可用电压/推力解；不能据此认定电池耗尽。")

    voltage_v = max(candidates)
    if not math.isfinite(voltage_v) or voltage_v <= 0:
        raise PowerLimitError("电池增量模型无正电压解。")
    propulsion_point = propulsion_map.operate(voltage_v, params.thrust_per_rotor_g)
    propulsion_power_w = propulsion_point.power_w * params.rotor_count
    total_power_w = propulsion_power_w + params.avionics_power_w
    current_a = total_power_w / voltage_v
    residual = voltage_v - baseline_voltage_v + resistance_ohm * (current_a - curve.reference_current_a)
    if not math.isfinite(current_a) or not math.isfinite(residual) or abs(residual) > 1e-6:
        raise PowerLimitError("电压求解未通过残差检查。")
    return voltage_v, current_a, propulsion_point, propulsion_power_w, total_power_w


def simulate(
    curve: DischargeCurve,
    propulsion_map: PropulsionMap,
    params: SimulationParams,
    cancel: Callable[[], bool] | None = None,
) -> SimulationResult:
    """显式 Euler 积分容量；每步检查截止事件，二分定位首次终止点。

    points 永远包含最终有效状态；汇总和 CSV 均从这一时序产生。
    cancel 由 GUI 的后台任务提供，不把任何界面对象传进模型。
    """

    _validate_params(params)
    capacity_ah = curve.usable_capacity_ah
    initial_consumed_ah = (1.0 - params.initial_soc) * capacity_ah
    resistance_ohm = params.equivalent_resistance_mohm / 1000.0
    # 固定推力下，推进表的电压层边界可换算成参考电池曲线上的容量节点。
    baseline_levels = []
    if not propulsion_map.is_single_voltage:
        for voltage in (propulsion_map.solver_voltages() if isinstance(propulsion_map, FittedPropulsionMap)
                        else propulsion_map.voltages):
            try:
                operating = propulsion_map.operate(voltage, params.thrust_per_rotor_g)
            except MapLimitError:
                continue
            power = operating.power_w * params.rotor_count + params.avionics_power_w
            baseline_levels.append(voltage + resistance_ohm * (power / voltage - curve.reference_current_a))
    charge_knots = curve.integration_knots_ah(baseline_levels)

    def sample(time_s: float, consumed_ah: float) -> SimulationPoint:
        """同一时刻的所有输出共享一次电池/推进联立求解。"""
        baseline = curve.voltage_at_consumed_ah(consumed_ah)
        voltage, current, prop, power, total = _solve_flight_point(
            baseline, curve, propulsion_map, params, resistance_ohm)
        return SimulationPoint(
            time_s=time_s, consumed_ah=consumed_ah, soc=max(0.0, 1 - consumed_ah / capacity_ah),
            baseline_voltage_v=baseline, voltage_v=voltage, current_a=current,
            total_thrust_g=params.total_thrust_g, thrust_per_rotor_g=params.thrust_per_rotor_g,
            rpm=prop.rpm, efficiency_g_per_w=prop.efficiency_g_per_w,
            propulsion_power_w=power, total_power_w=total,
            avionics_power_w=params.avionics_power_w,
            propulsion_current_a=power / voltage, avionics_current_a=params.avionics_power_w / voltage,
            model_extrapolated=(propulsion_map.is_extrapolated(voltage, params.thrust_per_rotor_g)
                                if isinstance(propulsion_map, FittedPropulsionMap) else False),
            rpm_is_estimate=isinstance(propulsion_map, FittedPropulsionMap),
        )

    def limit(point: SimulationPoint) -> str:
        if point.voltage_v <= params.cutoff_voltage_v:
            return "达到带载截止电压"
        if params.current_limit_a > 0 and point.current_a >= params.current_limit_a:
            return "达到电流限制"
        return ""

    points = [sample(0.0, initial_consumed_ah)]
    energy_wh = 0.0
    propulsion_energy_wh = 0.0
    avionics_energy_wh = 0.0
    kind = "limit"
    while True:
        current = points[-1]
        cutoff_reason = limit(current)
        if cutoff_reason:
            break
        if current.consumed_ah >= capacity_ah:
            cutoff_reason, kind = "达到参考放电曲线末端（数据边界）", "capacity"
            break
        if current.time_s >= params.max_time_s:
            cutoff_reason, kind = "达到最大仿真时间（尚未到截止条件）", "time"
            break
        if cancel and cancel():
            cutoff_reason, kind = "用户停止计算", "cancelled"
            break
        if len(points) > 200000:
            cutoff_reason, kind = "达到 200000 步计算上限（尚未到截止条件）", "steps"
            break
        # 一阶时间积分；精度应通过减半步长的收敛检查评估。
        next_charge = charge_knots[bisect.bisect_right(charge_knots, current.consumed_ah)]
        capacity_step_s = (next_charge - current.consumed_ah) * 3600 / current.current_a
        step_s = min(params.time_step_s, params.max_time_s - current.time_s,
                     capacity_step_s)

        def advance(delta: float) -> SimulationPoint:
            # 到达容量节点时直接赋精确值，避免浮点尾差产生零长度时间步。
            charge = (next_charge if delta == capacity_step_s else
                      min(capacity_ah, current.consumed_ah + current.current_a * delta / 3600))
            return sample(current.time_s + delta, charge)

        try:
            following = advance(step_s)
            cutoff_reason = limit(following)
        except (MapLimitError, PowerLimitError) as exc:
            following = None
            cutoff_reason = str(exc)
            kind = "data" if isinstance(exc, MapLimitError) else "power"

        if cutoff_reason:
            # 事件在本步内发生：越界时保留最后有效点；阈值事件记录首个截止点。
            low, high = 0.0, step_s
            last_valid = current
            first_stopped = following
            for _ in range(50):
                if high - low <= 1e-7:
                    break
                middle = (low + high) / 2
                try:
                    candidate = advance(middle)
                    reason = limit(candidate)
                    candidate_kind = "limit"
                except (MapLimitError, PowerLimitError) as exc:
                    candidate, reason = None, str(exc)
                    candidate_kind = "data" if isinstance(exc, MapLimitError) else "power"
                if reason:
                    high, first_stopped, cutoff_reason = middle, candidate, reason
                    kind = candidate_kind
                else:
                    low, last_valid = middle, candidate
            following = first_stopped if first_stopped is not None else last_valid
            if first_stopped is not None:
                kind = "limit"
        if following.time_s <= current.time_s:
            if not cutoff_reason:
                raise ValueError("时间或容量分辨率不足，请调整步长和参数量级。")
            break
        duration_h = (following.time_s - current.time_s) / 3600
        propulsion_energy_wh += current.propulsion_power_w * duration_h
        avionics_energy_wh += current.avionics_power_w * duration_h
        energy_wh = propulsion_energy_wh + avionics_energy_wh
        points.append(following)
        if cutoff_reason:
            break

    first = points[0]
    last = points[-1]
    warnings = ["SOC 为参考测试窗口的剩余容量比例；交流内阻不能唯一确定直流增量电阻。"]
    warnings.extend(propulsion_map.warnings)
    extrapolated = [p for p in points if p.model_extrapolated]
    if extrapolated:
        warnings.append(f"本次含 {len(extrapolated)} 个外推样本，首次出现在 {extrapolated[0].time_s / 60:.3f} min；"
                        "外推状态已写入时序，整段结果不属于完全实测支持的预测。")
    if propulsion_map.is_single_voltage and propulsion_map.allow_single_voltage_approximation:
        warnings.append("单电压推进表按电压无关近似使用，不能验证低电压转速/推力极限。")
    warnings.append(f"本次电流范围 {min(p.current_a for p in points):.2f}～"
                    f"{max(p.current_a for p in points):.2f} A；参考测试 {curve.reference_current_a:g} A。")
    return SimulationResult(
        points=tuple(points),
        duration_s=last.time_s,
        cutoff_reason=cutoff_reason,
        capacity_ah=capacity_ah,
        consumed_ah=last.consumed_ah,
        end_soc=last.soc,
        end_voltage_v=last.voltage_v,
        average_current_a=((last.consumed_ah - initial_consumed_ah) * 3600 / last.time_s
                           if last.time_s else last.current_a),
        peak_current_a=max(p.current_a for p in points),
        average_power_w=energy_wh * 3600 / last.time_s if last.time_s else last.total_power_w,
        start_rpm=first.rpm,
        end_rpm=last.rpm,
        start_propulsion_power_w=first.propulsion_power_w,
        end_propulsion_power_w=last.propulsion_power_w,
        total_mass_g=params.total_mass_g,
        total_thrust_g=params.total_thrust_g,
        used_energy_wh=energy_wh,
        min_current_a=min(p.current_a for p in points),
        termination_kind=kind,
        warnings=tuple(warnings),
        propulsion_energy_wh=propulsion_energy_wh,
        avionics_energy_wh=avionics_energy_wh,
        avionics_power_w=params.avionics_power_w,
    )


def _validate_params(params: SimulationParams) -> None:
    """在模型边界校验输入；同时保护直接调用模型的非 GUI 调用者。"""

    for field in fields(params):
        value = getattr(params, field.name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{field.name} 必须为有限数字。")
    if params.airframe_mass_g < 0 or params.battery_mass_g < 0 or params.payload_mass_g < 0:
        raise ValueError("机体、电池和载荷质量不能为负数。")
    if params.total_mass_g <= 0:
        raise ValueError("总质量必须大于 0 g。")
    if params.rotor_count < 1 or int(params.rotor_count) != params.rotor_count:
        raise ValueError("旋翼数量必须为正整数。")
    if params.thrust_to_weight < 1:
        raise ValueError("悬停等效推力系数不能小于 1；大于 1 仅作经验修正。")
    if params.avionics_power_w < 0:
        raise ValueError("用电器功率不能为负数。")
    if params.equivalent_resistance_mohm < 0:
        raise ValueError("等效内阻不能为负数。")
    if params.cutoff_voltage_v <= 0:
        raise ValueError("截止电压必须大于 0 V。")
    if not 0 < params.initial_soc <= 1:
        raise ValueError("初始 SOC 必须在 0% 到 100% 之间。")
    if params.time_step_s <= 0 or params.max_time_s <= 0:
        raise ValueError("时间步长和最大仿真时间必须大于 0。")
    if params.current_limit_a < 0:
        raise ValueError("电流限制不能为负数；0 表示不限制。")
    if params.max_time_s / params.time_step_s > 200000:
        raise ValueError("仿真上限为 200000 步；请增大步长或缩短时间。")


def self_check() -> None:
    """用内置演示数据做最小可运行自检，不代表真实性能验证。"""

    from .battery_model import create_demo_curve
    from .propulsion_model import create_demo_propulsion_map

    result = simulate(
        create_demo_curve(),
        create_demo_propulsion_map(),
        SimulationParams(
            airframe_mass_g=400,
            battery_mass_g=230,
            payload_mass_g=0,
            rotor_count=4,
            thrust_to_weight=1.0,
            avionics_power_w=5,
            equivalent_resistance_mohm=20,
            cutoff_voltage_v=9.0,
            initial_soc=1.0,
            time_step_s=1.0,
            max_time_s=7200,
        ),
    )
    assert result.points
    assert result.duration_s > 0
    assert result.total_mass_g == 630
    assert result.start_rpm > 0


if __name__ == "__main__":
    self_check()
    print("simulation self-check passed")
