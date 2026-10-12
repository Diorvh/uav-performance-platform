"""电池模型。

当前版本使用一条恒流 10 A 放电曲线作为电压基线，再用等效电阻修正
实际电流偏离参考电流时的瞬时电压变化：

    V = V_10A(q) - R_eff * (I - I_ref)

其中 q 是按实际电流累计的已消耗容量。这个模型适合固定温度、固定电池
和电流接近参考电流的悬停续航预测。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CurvePoint:
    """放电曲线中的一个测量点。"""

    time_s: float
    voltage_v: float


class DischargeCurve:
    """把恒流放电电压曲线转换为可按已消耗 Ah 查询的基线模型。"""

    def __init__(self, points: Iterable[CurvePoint], reference_current_a: float):
        if not math.isfinite(reference_current_a) or reference_current_a <= 0:
            raise ValueError("参考放电电流必须大于 0 A。")

        cleaned = [CurvePoint(float(p.time_s), float(p.voltage_v)) for p in points]
        if len(cleaned) < 2:
            raise ValueError("放电曲线至少需要两个有效数据点。")
        if any(not math.isfinite(p.time_s) or not math.isfinite(p.voltage_v) for p in cleaned):
            raise ValueError("放电曲线不能含 NaN 或无穷大。")
        if cleaned[0].time_s < 0:
            raise ValueError("放电时间不能为负数。")
        if any(point.voltage_v <= 0 for point in cleaned):
            raise ValueError("放电曲线中的电压必须大于 0 V。")

        # 不排序或吞掉重复点：时间重置通常意味着混入了另一段测试。
        if any(b.time_s <= a.time_s for a, b in zip(cleaned, cleaned[1:])):
            raise ValueError("放电时间必须严格递增；请处理重复时间或分段测试。")

        self.points = tuple(cleaned)
        self.reference_current_a = reference_current_a
        self._times = tuple(point.time_s for point in self.points)
        self._voltages = tuple(point.voltage_v for point in self.points)
        self._charges_ah = tuple(
            reference_current_a * (time_s - self._times[0]) / 3600.0
            for time_s in self._times
        )
        if not math.isfinite(self.usable_capacity_ah) or self.usable_capacity_ah <= 0:
            raise ValueError("参考电流与时长无法形成有效容量。")

    @property
    def duration_s(self) -> float:
        """返回实测放电时长。"""

        return self._times[-1] - self._times[0]

    @property
    def usable_capacity_ah(self) -> float:
        """按参考电流和实测时长估计的可用容量。"""

        return self.reference_current_a * self.duration_s / 3600.0

    @property
    def start_voltage_v(self) -> float:
        return self._voltages[0]

    @property
    def end_voltage_v(self) -> float:
        return self._voltages[-1]

    def voltage_at_consumed_ah(self, consumed_ah: float) -> float:
        """按已消耗容量线性插值查询 10 A 基线电压。"""

        if not math.isfinite(consumed_ah) or not 0 <= consumed_ah <= self.usable_capacity_ah:
            raise ValueError("已消耗容量超出实测曲线范围；不允许电池曲线外推。")
        if consumed_ah <= self._charges_ah[0]:
            return self._voltages[0]
        if consumed_ah >= self._charges_ah[-1]:
            return self._voltages[-1]

        index = bisect.bisect_left(self._charges_ah, consumed_ah)
        q1 = self._charges_ah[index - 1]
        q2 = self._charges_ah[index]
        ratio = (consumed_ah - q1) / (q2 - q1)
        return self._voltages[index - 1] + ratio * (
            self._voltages[index] - self._voltages[index - 1]
        )

    def integration_knots_ah(self, voltage_levels: Iterable[float] = ()) -> tuple[float, ...]:
        """容量积分不跨过原始曲线拐点，也不跨过指定基线电压的交点。

        仿真器可传入推进电压层换算的基线电压，防止一步跳过电流峰值或电压谷值。
        这只细分步长，不增加或拟合任何测量值。
        """
        knots = set(self._charges_ah)
        levels = tuple(voltage_levels)
        for q1, q2, v1, v2 in zip(self._charges_ah, self._charges_ah[1:], self._voltages, self._voltages[1:]):
            if v1 == v2:
                continue
            for level in levels:
                if min(v1, v2) < level < max(v1, v2):
                    knots.add(q1 + (q2 - q1) * (level - v1) / (v2 - v1))
        return tuple(sorted(knots))


def create_demo_curve(reference_current_a: float = 10.0) -> DischargeCurve:
    """创建仅用于检查界面的演示曲线，不代表真实电池数据。"""

    data = (
        (0, 12.60),
        (60, 12.42),
        (180, 12.25),
        (360, 12.12),
        (600, 11.98),
        (900, 11.82),
        (1200, 11.68),
        (1500, 11.54),
        (1800, 11.38),
        (2040, 11.20),
        (2160, 10.95),
        (2220, 10.60),
    )
    return DischargeCurve((CurvePoint(t, v) for t, v in data), reference_current_a)
