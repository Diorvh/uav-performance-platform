from __future__ import annotations

from dataclasses import dataclass


Y_FIELDS = {
    "力效 (g/W)": ("efficiency_g_per_w", "力效 (g/W)"),
    "拉力 (g)": ("thrust_g", "拉力 (g)"),
    "拉力 (mN)": ("thrust_mn", "拉力 (mN)"),
    "功率 (W)": ("power_w", "功率 (W)"),
    "电流 (A)": ("current_a", "电流 (A)"),
    "电压 (V)": ("voltage_v", "电压 (V)"),
    "转速 (rpm)": ("rpm", "转速 (rpm)"),
}
X_FIELDS = {
    "油门 (μs)": ("throttle_us", "油门 (μs)"),
    **Y_FIELDS,
}

POINT_COLUMNS = (
    ("group_name", "数据组", 220),
    ("throttle_us", "油门", 70),
    ("voltage_v", "电压/V", 75),
    ("current_a", "电流/A", 75),
    ("rpm", "转速", 80),
    ("thrust_mn", "拉力/mN", 85),
    ("power_w", "功率/W", 80),
    ("thrust_g", "拉力/g", 80),
    ("efficiency_g_per_w", "力效/(g/W)", 95),
)


@dataclass(frozen=True)
class MeasurementCandidate:
    point_order: int
    throttle_us: float
    voltage_v: float | None
    current_a: float | None
    rpm: float | None
    thrust_mn: float | None
    power_w: float | None
    thrust_g: float | None
    efficiency_g_per_w: float | None
    source_row: int


@dataclass(frozen=True)
class GroupCandidate:
    source_order: int
    motor_spec: str
    test_date: str | None
    propeller: str | None
    nominal_voltage_v: float | None
    esc_advance_deg: float | None
    pwm_frequency_khz: float | None
    am32_motor_kv: int | None
    no_load_current_a: float | None
    source_file: str
    source_workbook_index: int
    source_sheet: str
    source_sheet_index: int
    source_block_index: int
    source_anchor: str
    points: tuple[MeasurementCandidate, ...]
