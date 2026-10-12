"""共享电压指数的净力效模型；从选中实测数据校准，不覆盖数据库。

eta=A*(T/300)**c*(V/9)**b。T、eta 使用标准 gf 与 gf/W，
所以 A 由转换后的数据重拟合，不能直接混用历史 /9.8 的系数。
"""

from __future__ import annotations

import math

from .propulsion_model import MapLimitError, PropulsionMap, PropulsionOperatingPoint


SHARED_VOLTAGE_EXPONENT = -0.1117351442


class FittedPropulsionMap(PropulsionMap):
    """A、c 对所选硬件校准，b 使用历史十族探索值；不宣称普适性。"""

    def __init__(self, source: PropulsionMap, *, allow_extrapolation: bool = False):
        super().__init__(source.points, allow_single_voltage_approximation=False)
        if len(self.voltages) < 2 or any(len(layer) < 3 for layer in self._slices.values()):
            raise ValueError("共享指数拟合需至少两个电压层，每层至少三个点；单点组不参与主拟合。")
        overlap_low = max(layer[0].thrust_g for layer in self._slices.values())
        overlap_high = min(layer[-1].thrust_g for layer in self._slices.values())
        if overlap_high - overlap_low < 50 * 9.8 / 9.80665:
            raise ValueError("拟合各电压层的共同推力区间需至少约 50 gf，请重新选择实验组。")
        self.source_map = source
        self.allow_extrapolation = allow_extrapolation
        self.b = SHARED_VOLTAGE_EXPONENT
        xs = [math.log(p.thrust_g / 300) for p in self.points]
        ys = [math.log(p.efficiency_g_per_w) - self.b * math.log(p.voltage_v / 9)
              for p in self.points]
        x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
        variance = sum((x - x_mean) ** 2 for x in xs)
        if variance <= 1e-15:
            raise ValueError("推力变化不足，无法标定力效的推力指数。")
        self.c = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / variance
        self.a = math.exp(y_mean - self.c * x_mean)
        # 过原点最小二乘标定 T=k*n²；它只估算所需转速，不提供最大转速限制。
        self.rpm_coefficient = (sum(p.rpm**2 * p.thrust_g for p in self.points)
                                / sum(p.rpm**4 for p in self.points))
        self.fit_mape_percent = 100 * sum(
            abs(self.efficiency(p.voltage_v, p.thrust_g) / p.efficiency_g_per_w - 1)
            for p in self.points) / len(self.points)
        self.rpm_mape_percent = 100 * sum(
            abs(math.sqrt(p.thrust_g / self.rpm_coefficient) / p.rpm - 1)
            for p in self.points) / len(self.points)
        self.warnings = (
            "共享指数净力效拟合：A、c 由选中数据校准，b 来自历史十族探索；样本内误差不是续航误差。",
            f"本次校准样本内力效 MAPE={self.fit_mape_percent:.2f}%；转速 MAPE={self.rpm_mape_percent:.2f}%。",
            "转速为 T=k×rpm² 估算；固定推力下不随电压变化，未建模低压最大推力/转速及实飞动态。",
            "允许 7.5～12.6 V 电压外推；推力仍限所选实测总体范围，超出插值支持的样本会单独标记。"
            if allow_extrapolation else "仅在选中实测表的电压/推力插值支持内使用拟合模型。",
            "动力采用扣空载净功率；整机用电器手动另加一次，不自动补回空载项。",
        )

    def efficiency(self, voltage_v: float, thrust_g: float) -> float:
        return self.a * (thrust_g / 300) ** self.c * (voltage_v / 9) ** self.b

    def is_extrapolated(self, voltage_v: float, thrust_g: float) -> bool:
        try:
            self.source_map.operate(voltage_v, thrust_g)
        except MapLimitError:
            return True
        return False

    def operate(self, voltage_v: float, thrust_g: float) -> PropulsionOperatingPoint:
        if not all(math.isfinite(v) and v > 0 for v in (voltage_v, thrust_g)):
            raise ValueError("目标电压和推力必须为有限正数。")
        if self.allow_extrapolation:
            if not 7.5 <= voltage_v <= 12.6:
                raise MapLimitError("拟合模型电压外推仅限 7.5～12.6 V；到达此边界不是电池耗尽。")
            if not self.min_thrust_g <= thrust_g <= self.max_thrust_g:
                raise MapLimitError(f"拟合推力限所选实测总体范围 {self.min_thrust_g:.1f}～{self.max_thrust_g:.1f} gf。")
        else:
            self.source_map.operate(voltage_v, thrust_g)
        efficiency = self.efficiency(voltage_v, thrust_g)
        power = thrust_g / efficiency
        rpm = math.sqrt(thrust_g / self.rpm_coefficient)
        if not all(math.isfinite(v) and v > 0 for v in (efficiency, power, rpm)):
            raise ValueError("拟合模型产生了非有限或非正工作点。")
        return PropulsionOperatingPoint(voltage_v, rpm, thrust_g, power, efficiency)

    def solver_voltages(self) -> tuple[float, ...]:
        return tuple(sorted(set(self.voltages + ((7.5, 12.6) if self.allow_extrapolation else ()))))

    def metadata(self) -> dict:
        return {"type": "shared_voltage_power_law", "formula": "eta=A*(T_gf/300)^c*(V/9)^b",
                "A": self.a, "c": self.c, "b": self.b, "thrust_unit": "standard gf",
                "calibration": "OLS of log eta minus fixed b*log(V/9); equal point weights",
                "b_source": "historical 10 families / 89 points; not refitted in this run",
                "sample_count": len(self.points), "sample_mape_percent": self.fit_mape_percent,
                "rpm_model": "T_gf=k*rpm^2; origin-constrained least squares",
                "rpm_k": self.rpm_coefficient, "rpm_sample_mape_percent": self.rpm_mape_percent,
                "allow_extrapolation": self.allow_extrapolation,
                "extrapolation_voltage_limits_v": [7.5, 12.6],
                "thrust_limits_gf": [self.min_thrust_g, self.max_thrust_g]}

    def describe(self, source: str) -> str:
        return (super().describe(source) +
                f"\n共享指数拟合：A={self.a:.6g}，c={self.c:.6g}，b={self.b:.6g}"
                f"\n样本内力效误差 {self.fit_mape_percent:.2f}% · 转速为估算"
                + ("\n启用 7.5～12.6 V 电压外推（未验证）" if self.allow_extrapolation else "\n仅限实测插值支持"))

    def solve_voltage(self, baseline: float, reference_current: float, resistance: float,
                      thrust: float, rotors: int, avionics: float) -> float:
        """用严格凸的残差函数求高压分支；无解与超出数据支持分别处理。

        固定推力下 P=C*V^d+Pa，d=-b∈(0,1)。
        F(V)=V-E+R*(C*V^(d-1)+Pa/V)，F''>0。
        先定位唯一极小值，再在右侧二分，避免漏掉同号端点之间的双根。
        容量仍用显式积分；这里只求当前时刻的代数约束。
        """
        if resistance == 0:
            return baseline
        d = -self.b
        coefficient = rotors * thrust / self.efficiency(1.0, thrust)
        source = baseline + resistance * reference_current

        def residual(v):
            return v - source + resistance * (coefficient * v ** (d - 1) + avionics / v)

        def derivative(v):
            return 1 + resistance * (coefficient * (d - 1) * v ** (d - 2) - avionics / v**2)

        # E 以上 F>0。若到 E 仍在下降，E 以下也不存在零点。
        if derivative(source) <= 0:
            raise ValueError("电池增量模型无可用高电压功率解。")
        lo, hi = 0.0, source
        for _ in range(100):
            mid = (lo + hi) / 2
            if derivative(mid) < 0:
                lo = mid
            else:
                hi = mid
        minimum = (lo + hi) / 2
        if residual(minimum) > 1e-9:
            raise ValueError("电池增量模型无可用高电压功率解。")
        lo, hi = minimum, source
        for _ in range(100):
            mid = (lo + hi) / 2
            if residual(mid) > 0:
                hi = mid
            else:
                lo = mid
        voltage = (lo + hi) / 2
        self.operate(voltage, thrust)  # 不能把数据范围外的数学根当作有效工作点。
        return voltage
