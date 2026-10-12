"""数据库适配回归检查。建库/改标记仅发生在临时测试库，不触及实测数据库。"""

import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .database_adapter import GROUP_COLUMNS, MEASUREMENT_COLUMNS, build_propulsion, read_database
from .io_utils import save_run_record
from .propulsion_model import MapLimitError
from .simulation import simulate
from .test_simulation import PARAMS, curve


def make_database(path):
    """最小兼容模式，不依赖数据库项目包的迁移或可写构造函数。"""
    with closing(sqlite3.connect(path)) as con, con:
        for table, columns in (("test_groups", GROUP_COLUMNS), ("measurements", MEASUREMENT_COLUMNS)):
            con.execute(f"CREATE TABLE {table} ({', '.join(columns)})")


def add_group(path, group_id, order, voltage=10, count=2, **overrides):
    """源字段与库内扣空载派生字段同时保留，以检查二者不被混用。"""
    group = dict.fromkeys(GROUP_COLUMNS)
    group.update(id=group_id, display_order=order, motor_spec="测试电机", propeller="测试桨",
                 nominal_voltage_v=voltage, esc_advance_deg=15, pwm_frequency_khz=48, am32_motor_kv=820,
                 no_load_current_a=0.1, analysis_preferred=0, source_file="test.xlsx", source_sheet="sheet",
                 source_workbook_index=1, source_sheet_index=1, source_block_index=1, source_anchor="B3")
    group.update(overrides)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(f"INSERT INTO test_groups ({','.join(GROUP_COLUMNS)}) VALUES ({','.join('?' for _ in GROUP_COLUMNS)})",
                    tuple(group[k] for k in GROUP_COLUMNS))
        for i in range(1, count + 1):
            raw = dict.fromkeys(MEASUREMENT_COLUMNS)
            mn = 100 * i * 9.80665
            power = voltage * (2 * i - group["no_load_current_a"])
            raw.update(id=group_id * 10 + i, group_id=group_id, point_order=i, source_row=i + 3,
                       throttle_us=1000 + i * 100, voltage_v=voltage, current_a=2 * i, rpm=i * 1000,
                       thrust_mn=mn, thrust_g=mn / 9.8, power_w=power, efficiency_g_per_w=mn / 9.8 / power)
            con.execute(f"INSERT INTO measurements ({','.join(MEASUREMENT_COLUMNS)}) VALUES ({','.join('?' for _ in MEASUREMENT_COLUMNS)})",
                        tuple(raw[k] for k in MEASUREMENT_COLUMNS))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "data.sqlite3"
        make_database(self.path)

    def test_read_only_uri_and_missing_path_not_created(self):
        add_group(self.path, 99, 76)
        before = self.path.read_bytes()
        connect = sqlite3.connect
        with patch("simulation.database_adapter.sqlite3.connect", wraps=connect) as mocked:
            read_database(self.path)
        self.assertIn("mode=ro", mocked.call_args.args[0])
        self.assertEqual(self.path.read_bytes(), before)
        absent = self.path.with_name("absent.sqlite3")
        with self.assertRaises(FileNotFoundError):
            read_database(absent)
        self.assertFalse(absent.exists())

    def test_group_id_not_display_order_and_net_power(self):
        add_group(self.path, 99, 76)
        snapshot = read_database(self.path)
        selected = build_propulsion(snapshot, [99])
        p = selected.propulsion_map.points[0]
        self.assertEqual(p.thrust_g, 100)
        self.assertEqual(p.power_w, 19)
        self.assertAlmostEqual(p.efficiency_g_per_w, 100 / 19)
        self.assertEqual(selected.provenance["measurements"][0]["power_measured_total_w"], 20)
        self.assertAlmostEqual(selected.provenance["measurements"][0]["current_propulsion_net_a"], 1.9)
        raw = selected.provenance["measurements"][0]["raw"]
        self.assertAlmostEqual(raw["power_w"], 19)
        self.assertEqual(raw["id"], 991)
        with self.assertRaises(ValueError):
            build_propulsion(snapshot, [76])

    def test_adoption_rule_and_conflicts_are_local(self):
        add_group(self.path, 1, 1)
        add_group(self.path, 2, 2, repeat_set=1)
        add_group(self.path, 3, 3, repeat_set=1)
        add_group(self.path, 4, 4, repeat_set=2, analysis_preferred=1)
        add_group(self.path, 5, 5, repeat_set=2, analysis_preferred=1)
        snapshot = read_database(self.path)
        self.assertEqual([g["id"] for g in snapshot.eligible_groups], [1])
        self.assertEqual(snapshot.status(snapshot.groups[1]), "待人工采用")
        self.assertEqual(snapshot.status(snapshot.groups[3]), "采用冲突")
        build_propulsion(snapshot, [1])
        for group_id in (2, 4):
            with self.assertRaises(ValueError):
                build_propulsion(snapshot, [group_id])

    def test_preferred_group_only_and_invalid_flag(self):
        add_group(self.path, 1, 1, repeat_set=7)
        add_group(self.path, 2, 2, repeat_set=7, analysis_preferred=1)
        add_group(self.path, 3, 3, analysis_preferred=2)
        snapshot = read_database(self.path)
        self.assertEqual([g["id"] for g in snapshot.eligible_groups], [2])

    def test_i0_may_change_across_voltage_without_changing_hardware(self):
        add_group(self.path, 1, 76, voltage=8.1, no_load_current_a=0.095)
        add_group(self.path, 2, 77, voltage=11.1, no_load_current_a=0.075)
        selected = build_propulsion(read_database(self.path), [1, 2])
        expected = 0.7 * 8.1 * (3 - 0.095) + 0.3 * 11.1 * (3 - 0.075)
        self.assertAlmostEqual(selected.propulsion_map.operate(9, 150).power_w, expected)

    def test_mixed_hardware_and_same_voltage_curves_rejected(self):
        add_group(self.path, 1, 1)
        add_group(self.path, 2, 2, voltage=12, pwm_frequency_khz=96)
        add_group(self.path, 3, 3, no_load_current_a=0.2)
        snapshot = read_database(self.path)
        for selection in ([1, 2], [1, 3]):
            with self.assertRaises(ValueError):
                build_propulsion(snapshot, selection)

    def test_single_point_retained_but_not_promoted_to_curve(self):
        add_group(self.path, 1, 76)
        add_group(self.path, 2, 78, voltage=7.5, count=1)
        snapshot = read_database(self.path)
        self.assertEqual(len(snapshot.measurements), 3)
        self.assertEqual(len(snapshot.eligible_groups), 2)
        with self.assertRaisesRegex(ValueError, "078 组仅 1 个点"):
            build_propulsion(snapshot, [1, 2])

    def test_no_database_voltage_extrapolation_even_with_one_layer(self):
        add_group(self.path, 1, 1)
        data = build_propulsion(read_database(self.path), [1]).propulsion_map
        self.assertEqual(data.operate(10, 150).power_w, 29)
        with self.assertRaises(MapLimitError):
            data.operate(11, 150)
        with self.assertRaises(MapLimitError):
            data.operate(10, 250)

    def test_invalid_raw_measurement_is_not_skipped(self):
        add_group(self.path, 1, 1)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE measurements SET current_a=NULL WHERE id=11")
        with self.assertRaisesRegex(ValueError, "measurement_id=11"):
            build_propulsion(read_database(self.path), [1])

    def test_stale_derived_columns_not_used_for_power(self):
        add_group(self.path, 1, 1)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE measurements SET power_w=-999, efficiency_g_per_w=NULL, thrust_g=NULL")
        data = build_propulsion(read_database(self.path), [1]).propulsion_map
        self.assertEqual(data.points[0].power_w, 19)
        self.assertEqual(data.points[0].thrust_g, 100)

    def test_snapshot_hash_detects_mark_changes(self):
        add_group(self.path, 1, 1)
        first = read_database(self.path)
        self.assertEqual(first.sha256, read_database(self.path).sha256)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE test_groups SET repeat_set=1")
        self.assertNotEqual(first.sha256, read_database(self.path).sha256)

    def test_missing_schema_and_orphan_rejected(self):
        add_group(self.path, 1, 1)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE measurements SET group_id=99")
        with self.assertRaisesRegex(ValueError, "外键"):
            read_database(self.path)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("DROP TABLE measurements")
        with self.assertRaisesRegex(ValueError, "缺少字段"):
            read_database(self.path)

    def test_end_to_end_and_traceable_record(self):
        add_group(self.path, 1, 76, voltage=8)
        add_group(self.path, 2, 77, voltage=12)
        selected = build_propulsion(read_database(self.path), [1, 2])
        battery = curve(11, 9)
        result = simulate(battery, selected.propulsion_map, PARAMS)
        expected_time = 3600 / (4 * (3 - 0.1))  # 扣空载后单桨2.9A；用电器0W，不自动补回I0。
        self.assertAlmostEqual(result.duration_s, expected_time)
        record = self.path.with_suffix(".json")
        save_run_record(result, PARAMS, battery, selected.propulsion_map, record, selected.provenance, (self.path,))
        data = json.loads(record.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["provenance"]["selected_groups"][0]["display_order"], 76)
        self.assertEqual(data["provenance"]["measurements"][0]["raw"]["source_row"], 4)
        self.assertEqual(data["params"]["rotor_count"], 4)
        self.assertAlmostEqual(data["summary"]["duration_s"], expected_time)
        self.assertEqual(data["record_format_version"], 3)
        self.assertFalse(data["power_accounting"]["automatic_no_load_add_back"])
        with self.assertRaises(ValueError):
            save_run_record(result, PARAMS, battery, selected.propulsion_map, self.path, {}, (self.path,))

    def test_missing_idle_current_and_nonpositive_net_current_rejected(self):
        add_group(self.path, 1, 1)
        for value in (None, -0.1, 2):
            with closing(sqlite3.connect(self.path)) as con, con:
                con.execute("UPDATE test_groups SET no_load_current_a=?", (value,))
            with self.subTest(idle=value), self.assertRaises(ValueError):
                build_propulsion(read_database(self.path), [1])


if __name__ == "__main__":
    unittest.main()
