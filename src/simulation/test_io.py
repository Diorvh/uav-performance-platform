"""CSV 输入、单位和文件保护的回归检查，文件只写入临时目录。"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .io_utils import load_curve_csv, load_propulsion_csv, save_result_csv
from .simulation import simulate
from .test_simulation import PARAMS, curve, propulsion


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "input.csv"

    def write(self, text, encoding="utf-8-sig"):
        """创建隔离的输入样本，不触及用户数据。"""
        self.path.write_text(text, encoding=encoding)
        return self.path

    def test_time_and_voltage_unit_conversion(self):
        for header, end in (("time_min", 1), ("time_ms", 60000), ("time_h", 1 / 60)):
            self.write(f"{header},voltage_mV,unrelated_ms\n0,12000,0\n{end},11000,0\n")
            data = load_curve_csv(self.path, 10)
            self.assertAlmostEqual(data.duration_s, 60)
            self.assertEqual(data.end_voltage_v, 11)

    def test_gbk_chinese_semicolon(self):
        self.write("时间（分钟）;电压（V）\n0;12\n1;11", "gbk")
        self.assertEqual(load_curve_csv(self.path, 10).duration_s, 60)

    def test_headerless_exactly_two_columns(self):
        self.write("0,12\n60,11")
        self.assertEqual(load_curve_csv(self.path, 10).duration_s, 60)
        with self.assertRaises(ValueError):
            load_curve_csv(self.write("0,1,12\n60,2,11"), 10)

    def test_bad_row_is_not_silently_dropped(self):
        for body in ("time_s,voltage_V\n0,12\n1,bad\n2,11", "time_s,voltage_V\n0,12\n1",
                     "time_s,voltage_V\n0,12\n1,nan"):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "第 3 行"):
                load_curve_csv(self.write(body), 10)

    def test_unknown_or_ambiguous_units_rejected(self):
        for body in ("time_days,voltage_V\n0,12\n1,11", "time_s,time_min,voltage_V\n0,0,12\n1,1,11"):
            with self.assertRaises(ValueError):
                load_curve_csv(self.write(body), 10)

    def test_kw_and_newton_conversion(self):
        data = load_propulsion_csv(self.write("rpm,power_kW,thrust_N\n1000,0.02,0.980665\n2000,0.04,1.96133"), 12)
        self.assertAlmostEqual(data.points[0].thrust_g, 100)
        self.assertEqual(data.points[0].power_w, 20)

    def test_unrelated_kw_header_does_not_rescale_power(self):
        data = load_propulsion_csv(self.write("rpm,power_W,thrust_g,note_kw\n1000,20,100,x\n2000,40,200,y"), 12)
        self.assertEqual(data.points[0].power_w, 20)

    def test_shaft_conversion_and_no_double_loss(self):
        self.write("rpm,power_W,thrust_g\n1000,20,100\n2000,40,200")
        data = load_propulsion_csv(self.path, 12, "shaft", 0.8, no_load_current_a=0.1)
        self.assertAlmostEqual(data.points[0].power_w, 25 - 12 * 0.1)
        self.assertAlmostEqual(data.points[0].efficiency_g_per_w, 100 / (25 - 12 * 0.1))
        with self.assertRaises(ValueError):
            load_propulsion_csv(self.path, 12, "electrical", 0.8)

    def test_net_csv_is_not_subtracted_twice(self):
        self.write("rpm,thrust_g,power_W,no_load_current_A\n1000,100,20,0.1\n2000,200,40,0.1")
        data = load_propulsion_csv(self.path, 12, "net")
        self.assertEqual(data.points[0].power_w, 20)
        data = load_propulsion_csv(self.path, 12, "electrical", no_load_current_a=999)
        self.assertAlmostEqual(data.points[0].power_w, 18.8)  # 逐行 I0 优先于后备值。

    def test_total_power_csv_needs_explicit_idle_current(self):
        self.write("rpm,thrust_g,power_W\n1000,100,20\n2000,200,40")
        with self.assertRaises(ValueError):
            load_propulsion_csv(self.path, 12, "electrical")
        data = load_propulsion_csv(self.path, 12, "electrical", no_load_current_a=0)
        self.assertEqual(data.points[0].power_w, 20)

    def test_optional_power_can_be_blank_but_not_zero_or_text(self):
        template = "rpm,thrust_g,power_W,efficiency_g_per_W\n1000,100,{},5\n2000,200,40,5"
        data = load_propulsion_csv(self.write(template.format("")), 12)
        self.assertEqual(data.points[0].power_w, 20)
        for bad in ("0", "bad", "NaN", "100"):
            with self.assertRaisesRegex(ValueError, "第 2 行"):
                load_propulsion_csv(self.write(template.format(bad)), 12)

    def test_thrust_columns_must_agree(self):
        with self.assertRaisesRegex(ValueError, "推力不一致"):
            load_propulsion_csv(self.write("rpm,power_W,thrust_g,thrust_N\n1000,20,100,9\n2000,40,200,2"), 12)

    def test_rounding_tolerance_uses_power_as_authority(self):
        data = load_propulsion_csv(self.write("rpm,thrust_g,power_W,efficiency_g_per_W\n1000,100,20,5.1\n2000,200,40,5"), 12)
        self.assertEqual(data.points[0].power_w, 20)
        self.assertEqual(data.points[0].efficiency_g_per_w, 5)

    def test_multivoltage_uses_its_own_voltage(self):
        body = "voltage_V,rpm,thrust_g,power_W\n10,1000,100,20\n10,2000,200,40\n12,1000,100,18\n12,2000,200,36"
        data = load_propulsion_csv(self.write(body), float("nan"))
        self.assertAlmostEqual(data.operate(11, 150).power_w, 28.5)

    def test_export_full_precision_and_protect_input(self):
        r = simulate(curve(), propulsion(), PARAMS)
        save_result_csv(r, self.path)
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(float(rows[-1]["time_s"]), r.duration_s)
        self.assertEqual(float(rows[-1]["voltage_v"]), r.end_voltage_v)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            save_result_csv(r, self.path, (self.path,))
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_replace_preserves_previous_file(self):
        self.write("original")
        before = self.path.read_bytes()
        with patch("simulation.io_utils.os.replace", side_effect=PermissionError("occupied")), self.assertRaises(PermissionError):
            save_result_csv(simulate(curve(), propulsion(), PARAMS), self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(Path(self.folder.name).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
