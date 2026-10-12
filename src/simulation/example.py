"""公开样例：通过常规接口读取明确指定的实验，不写入个人模型库。"""
from platform_config import require_database
from .battery_library import curve_from_record, record_id
from .battery_pack import BatteryPack
from .cell_database import load_cell_test
from .database_adapter import read_database, build_propulsion


FLIGHT_VALUES = {
    "airframe_mass": "211", "payload_mass": "0", "rotor_count": "4",
    "thrust_to_weight": "1.05", "avionics_power": "5", "initial_soc": "100",
    "current_limit": "0", "time_step": "1", "max_time": "120",
}


def load_example():
    snapshot = read_database(require_database("force"))
    groups = {g["id"]: g for g in snapshot.groups}
    for group_id, voltage in ((65, 8.1), (66, 11.1)):
        group = groups.get(group_id, {})
        if (group.get("motor_spec"), group.get("propeller"), group.get("nominal_voltage_v")) != (
                "HSKRC 2205-1700kV", "GF 7036F-2", voltage):
            raise ValueError("样例力效实验缺失或身份变化，请使用配套发布数据库，或手动选择实验。")
    propulsion = build_propulsion(snapshot, [65, 66], model_kind="shared_fit", allow_extrapolation=True)
    record = load_cell_test(require_database("battery"), 139, 97)
    source = record["source"]
    cell = source["cell_metadata"]
    if (cell["form_factor"], cell["category_code"], cell["sample_code"], cell["model_name"]) != (
            "半固态", "半固态", "1", "10.6Ah") or source["test"]["test_date"] != "2026-01-09 13:36:54":
        raise ValueError("样例电芯实验身份变化，请使用配套发布数据库，或手动选择实验。")
    profile = BatteryPack(
        name="样例 · 3S1P 半固态 10.6Ah", cell_name=record["name"], series=3, parallel=1,
        measured_mass_g=346, cell_resistance_mohm=0, wiring_resistance_mohm=14,
        cell_cutoff_v=2.5, cell_data_id=record_id(record),
        notes="单电芯实测容量约 10.8109 Ah；14 mΩ 为整包线束电阻，非电芯交流内阻。")
    provenance = {"profile_kind": "built_in_example", "library_path": "", "profile_id": "semisolid-3s1p",
                  "revision": 1, "profile": profile.metadata(), "cell_source": record}
    return profile, profile.build_curve(curve_from_record(record)), provenance, propulsion
