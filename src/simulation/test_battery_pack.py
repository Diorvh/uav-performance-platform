"""电池组换算与模型库存取用例；仅使用合成曲线和临时模型库。"""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from .battery_model import CurvePoint, DischargeCurve
from .battery_pack import BatteryPack
from .battery_library import BatteryLibrary, cell_record, record_id


def cell():
    return DischargeCurve([CurvePoint(0, 4.2), CurvePoint(3600, 3.0)], 1)


def pack():
    return BatteryPack("测试组", "测试电芯", 3, 2, cell_mass_g=45, extra_mass_g=20,
                       cell_resistance_mohm=20, cell_cutoff_v=3)


class BatteryPackTests(unittest.TestCase):
    def test_series_parallel_scaling_and_mass(self):
        profile = pack()
        curve = profile.build_curve(cell())
        self.assertEqual(curve.usable_capacity_ah, 2)
        self.assertEqual(curve.reference_current_a, 2)
        self.assertAlmostEqual(curve.voltage_at_consumed_ah(1), 10.8)
        self.assertEqual(profile.mass_g, 290)
        self.assertEqual(profile.resistance_mohm, 30)
        self.assertEqual(profile.cutoff_v, 9)

    def test_pack_equation_equals_cell_equation_with_wiring(self):
        profile = replace(pack(), wiring_resistance_mohm=5)
        curve = profile.build_curve(cell())
        current, charge = 4, 1
        voltage = curve.voltage_at_consumed_ah(charge) - profile.resistance_mohm / 1000 * (current - curve.reference_current_a)
        independent = 3 * (cell().voltage_at_consumed_ah(charge / 2) - .02 * (current / 2 - 1)) - .005 * current
        self.assertAlmostEqual(voltage, independent)

    def test_measured_mass_replaces_estimate(self):
        self.assertEqual(replace(pack(), measured_mass_g=310).mass_g, 310)

    def test_missing_curve_configuration_can_be_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "models.sqlite3"
            library = BatteryLibrary(path)
            self.assertEqual(library.load(), ({}, {}))
            self.assertFalse(path.exists())
            draft = replace(pack(), cell_resistance_mohm=None, cell_cutoff_v=None)
            key, _ = library.save(draft)
            loaded, sources = library.load()
            self.assertEqual(loaded[key][0], draft)
            self.assertFalse(sources)
            with self.assertRaises(ValueError):
                draft.build_curve(cell())

    def test_source_shared_and_stale_save_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            library = BatteryLibrary(Path(folder) / "models.sqlite3")
            source = cell_record(cell(), "测试电芯", {"type": "synthetic test"})
            profile = replace(pack(), cell_data_id=record_id(source))
            key, revision = library.save(profile, source=source)
            library.save(replace(profile, name="另一电池组", parallel=1), source=source)
            profiles, sources = library.load()
            self.assertEqual(len(profiles), 2)
            self.assertEqual(len(sources), 1)
            library.save(replace(profile, notes="修改"), key=key, revision=revision)
            with self.assertRaises(ValueError):
                library.save(profile, key=key, revision=revision)
            with self.assertRaises(ValueError):
                library.save(profile)


if __name__ == "__main__":
    unittest.main()
