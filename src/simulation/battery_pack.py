"""电池组配置和单电芯→整包换算，不依赖数据库或界面。"""

from dataclasses import asdict, dataclass
import math

from .battery_model import CurvePoint, DischargeCurve


@dataclass(frozen=True)
class BatteryPack:
    name: str
    cell_name: str
    series: int
    parallel: int
    cell_mass_g: float | None = None
    extra_mass_g: float = 0
    measured_mass_g: float | None = None
    cell_resistance_mohm: float | None = None
    wiring_resistance_mohm: float = 0
    cell_cutoff_v: float | None = None
    cell_data_id: str | None = None
    notes: str = ""

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.name, self.cell_name)):
            raise ValueError("请填写电池组名称和电芯型号。")
        if self.extra_mass_g is None or self.wiring_resistance_mohm is None:
            raise ValueError("附加重量和线束电阻不能为空；没有时填写 0。")
        for value in (self.series, self.parallel):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("串联数和并联数必须是正整数。")
        for label, value, positive in (
            ("单体重量", self.cell_mass_g, True), ("整包实测重量", self.measured_mass_g, True),
            ("附加重量", self.extra_mass_g, False), ("电芯增量电阻", self.cell_resistance_mohm, False),
            ("整包线束电阻", self.wiring_resistance_mohm, False), ("单体截止电压", self.cell_cutoff_v, True),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0 or (positive and value == 0)):
                raise ValueError(f"{label}必须为有限{'正' if positive else '非负'}数。")
        if self.measured_mass_g is None and self.cell_mass_g is None:
            raise ValueError("请填写整包实测重量，或填写单体重量用于估算。")
        if not math.isfinite(self.mass_g):
            raise ValueError("整包重量换算溢出。")

    @property
    def mass_g(self):
        if self.measured_mass_g is not None:
            return self.measured_mass_g
        return self.series * self.parallel * self.cell_mass_g + self.extra_mass_g

    @property
    def resistance_mohm(self):
        if self.cell_resistance_mohm is None:
            raise ValueError("尚未填写电芯增量电阻；不做修正时请明确填写 0。")
        return self.series / self.parallel * self.cell_resistance_mohm + self.wiring_resistance_mohm

    @property
    def cutoff_v(self):
        if self.cell_cutoff_v is None:
            raise ValueError("尚未填写单体带载截止电压。")
        return self.series * self.cell_cutoff_v

    def build_curve(self, cell_curve: DischargeCurve) -> DischargeCurve:
        """一致电芯、均匀分流；q包=Np*q单体，参考包电流=Np*参考单体电流。

        R包=Ns/Np*R单体+R线束；参考基线先扣参考包电流下的线束压降，
        再由求解器计算相对参考电流的增量压降，避免漏计或重复计入。
        """
        if not all(math.isfinite(x) for x in (self.resistance_mohm, self.cutoff_v)):
            raise ValueError("整包电阻或截止电压换算溢出。")
        reference = self.parallel * cell_curve.reference_current_a
        return DischargeCurve(
            [CurvePoint(p.time_s, self.series * p.voltage_v - reference * self.wiring_resistance_mohm / 1000)
             for p in cell_curve.points], reference)

    def metadata(self):
        return {**asdict(self), "effective_mass_g": self.mass_g,
                "mass_basis": "measured whole pack" if self.measured_mass_g is not None else "cell count plus extra",
                "pack_resistance_mohm": self.resistance_mohm,
                "pack_cutoff_v": self.cutoff_v,
                "assumption": "identical cells, equal parallel currents; test-window capacity, not absolute SOC"}
