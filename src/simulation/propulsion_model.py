"""推进系统和力效数据模型。

每个数据点默认代表单电机/单桨。输入至少要有：

    电压（可选）、转速、推力，以及功率或力效。

仿真时以当前电池电压和所需单桨推力查表，反求转速和单桨功率。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PropulsionPoint:
    """单电机/单桨数据点：power_w 为扣空载后的动力净功率，不是轴功率。"""

    voltage_v: float
    rpm: float
    thrust_g: float
    power_w: float
    efficiency_g_per_w: float


@dataclass(frozen=True)
class PropulsionOperatingPoint:
    """给定电压/推力的工作点；功率和力效均使用扣空载的动力净口径。"""

    voltage_v: float
    rpm: float
    thrust_g: float
    power_w: float
    efficiency_g_per_w: float


class MapLimitError(ValueError):
    """目标工况超出力效数据覆盖范围。"""


class PropulsionMap:
    """按电压分层、按单桨推力插值的推进系统地图。"""

    def __init__(self, points: Iterable[PropulsionPoint], *,
                 allow_single_voltage_approximation: bool = True, warnings: tuple[str, ...] = ()):
        self.allow_single_voltage_approximation = allow_single_voltage_approximation
        self.warnings = warnings
        cleaned = [
            PropulsionPoint(
                float(point.voltage_v),
                float(point.rpm),
                float(point.thrust_g),
                float(point.power_w),
                float(point.efficiency_g_per_w),
            )
            for point in points
        ]
        if len(cleaned) < 2:
            raise ValueError("力效数据至少需要两个有效数据点。")
        if any(not all(math.isfinite(x) for x in (p.voltage_v, p.rpm, p.thrust_g,
                   p.power_w, p.efficiency_g_per_w)) for p in cleaned):
            raise ValueError("力效数据不能含 NaN 或无穷大。")
        if any(point.voltage_v <= 0 or point.rpm <= 0 or point.thrust_g <= 0 for point in cleaned):
            raise ValueError("力效数据中的电压、转速和推力必须大于 0。")
        if any(point.power_w <= 0 or point.efficiency_g_per_w <= 0 for point in cleaned):
            raise ValueError("力效数据中的功率和力效必须大于 0。")
        if any(not math.isclose(p.efficiency_g_per_w, p.thrust_g / p.power_w,
                                rel_tol=0.03) for p in cleaned):
            raise ValueError("推力、功率和力效不一致（容许 3% 的表格舍入误差）。")
        # 以动力净功率为唯一推进能耗依据；用电器不在此表中重复计入。
        cleaned = [PropulsionPoint(p.voltage_v, p.rpm, p.thrust_g, p.power_w,
                                   p.thrust_g / p.power_w) for p in cleaned]

        # 以电压分组；同一电压层内按推力排序，便于反求转速。
        grouped: dict[float, list[PropulsionPoint]] = {}
        for point in cleaned:
            voltage_key = point.voltage_v
            grouped.setdefault(voltage_key, []).append(point)
        self._slices = {
            voltage: tuple(sorted(slice_points, key=lambda point: point.thrust_g))
            for voltage, slice_points in grouped.items()
        }
        for voltage, layer in self._slices.items():
            if len(layer) < 2:
                raise ValueError(f"{voltage:g} V 至少需要两个不同推力的点。")
            if any(b.thrust_g <= a.thrust_g or b.rpm <= a.rpm
                   for a, b in zip(layer, layer[1:])):
                raise ValueError(f"{voltage:g} V 的推力与转速必须一一对应、严格递增；请先处理重复或非单调测量。")
        self.points = tuple(cleaned)
        self.voltages = tuple(sorted(self._slices))
        self.min_thrust_g = min(point.thrust_g for point in cleaned)
        self.max_thrust_g = max(point.thrust_g for point in cleaned)

    @property
    def is_single_voltage(self) -> bool:
        return len(self.voltages) == 1

    def _operate_slice(self, voltage_v: float, thrust_g: float) -> PropulsionOperatingPoint:
        """在一层固定电压数据中按目标推力做线性插值。"""

        points = self._slices[voltage_v]
        if thrust_g < points[0].thrust_g or thrust_g > points[-1].thrust_g:
            raise MapLimitError(
                f"{voltage_v:.2f} V 力效数据的单桨推力范围为 "
                f"{points[0].thrust_g:.1f}～{points[-1].thrust_g:.1f} g，"
                f"当前需要 {thrust_g:.1f} g。"
            )

        for index in range(1, len(points)):
            left = points[index - 1]
            right = points[index]
            if thrust_g <= right.thrust_g:
                if math.isclose(thrust_g, left.thrust_g, abs_tol=1e-9):
                    return PropulsionOperatingPoint(
                        voltage_v,
                        left.rpm,
                        thrust_g,
                        left.power_w,
                        left.efficiency_g_per_w,
                    )
                span = right.thrust_g - left.thrust_g
                ratio = (thrust_g - left.thrust_g) / span
                power_w = left.power_w + ratio * (right.power_w - left.power_w)
                return PropulsionOperatingPoint(
                    voltage_v,
                    left.rpm + ratio * (right.rpm - left.rpm),
                    thrust_g,
                    power_w,
                    thrust_g / power_w,
                )
        return PropulsionOperatingPoint(
            voltage_v,
            points[-1].rpm,
            thrust_g,
            points[-1].power_w,
            points[-1].efficiency_g_per_w,
        )

    def operate(self, voltage_v: float, thrust_g: float) -> PropulsionOperatingPoint:
        """根据当前电压和目标单桨推力求推进工作点。"""

        if not math.isfinite(voltage_v) or voltage_v <= 0 or not math.isfinite(thrust_g) or thrust_g <= 0:
            raise ValueError("目标电压和推力必须是有限正数。")
        if self.is_single_voltage:
            if not self.allow_single_voltage_approximation and not math.isclose(
                    voltage_v, self.voltages[0], rel_tol=0, abs_tol=1e-9):
                raise MapLimitError(f"数据库仅有 {self.voltages[0]:g} V 电压层，不允许外推到 {voltage_v:g} V。")
            # 单电压只能近似恒定功率；需多电压台架数据才能描述电压依赖。
            point = self._operate_slice(self.voltages[0], thrust_g)
            return PropulsionOperatingPoint(voltage_v, point.rpm, thrust_g,
                                             point.power_w, thrust_g / point.power_w)
        if voltage_v < self.voltages[0] or voltage_v > self.voltages[-1]:
            raise MapLimitError(
                f"当前电池电压 {voltage_v:.2f} V 超出力效数据电压范围 "
                f"{self.voltages[0]:.2f}～{self.voltages[-1]:.2f} V。"
            )

        index = bisect.bisect_left(self.voltages, voltage_v)
        # 正好命中电压层时不检查无关的相邻层，避免误报推力越界。
        if index < len(self.voltages) and self.voltages[index] == voltage_v:
            return self._operate_slice(voltage_v, thrust_g)

        lower_voltage = self.voltages[index - 1]
        upper_voltage = self.voltages[index]
        lower = self._operate_slice(lower_voltage, thrust_g)
        upper = self._operate_slice(upper_voltage, thrust_g)
        ratio = (voltage_v - lower_voltage) / (upper_voltage - lower_voltage)
        power_w = lower.power_w + ratio * (upper.power_w - lower.power_w)
        return PropulsionOperatingPoint(
            voltage_v,
            lower.rpm + ratio * (upper.rpm - lower.rpm),
            thrust_g,
            power_w,
            thrust_g / power_w,
        )

    def voltage_segments(self, thrust_g: float) -> list[tuple[float, float]]:
        """给求解器返回两端均覆盖目标推力的连续电压区间；绝不跨数据缺口。"""
        return [(a, b) for a, b in zip(self.voltages, self.voltages[1:])
                if all(self._slices[v][0].thrust_g <= thrust_g <= self._slices[v][-1].thrust_g
                       for v in (a, b))]

    def describe(self, source: str) -> str:
        voltage_text = (
            f"{self.voltages[0]:.2f} V"
            if self.is_single_voltage
            else f"{self.voltages[0]:.2f}～{self.voltages[-1]:.2f} V"
        )
        return (
            f"来源：{source}\n"
            f"数据点：{len(self.points)}\n"
            f"电压范围：{voltage_text}\n"
            f"单桨总体范围：{self.min_thrust_g:.1f}～{self.max_thrust_g:.1f} gf（不代表每层均覆盖）"
        )


def create_demo_propulsion_map() -> PropulsionMap:
    """创建仅用于检查界面的演示力效数据。"""

    data = (
        (4000, 92.7, 16.1),
        (5000, 144.8, 12.9),
        (6000, 208.5, 10.7),
        (7000, 283.8, 9.2),
        (8000, 370.6, 8.1),
        (9000, 469.1, 7.2),
        (10000, 579.1, 6.45),
        (12000, 833.9, 5.37),
        (14000, 1135.0, 4.60),
        (16000, 1482.5, 4.03),
    )
    points = [
        PropulsionPoint(12.0, rpm, thrust_g, thrust_g / efficiency, efficiency)
        for rpm, thrust_g, efficiency in data
    ]
    return PropulsionMap(points)
