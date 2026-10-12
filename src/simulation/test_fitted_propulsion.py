"""共享指数模型的针对性用例；只用合成测点，不访问实测数据库或 GUI。"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from .battery_model import CurvePoint, DischargeCurve
from .fitted_propulsion import FittedPropulsionMap, SHARED_VOLTAGE_EXPONENT
from .io_utils import save_run_record
from .propulsion_model import MapLimitError, PropulsionMap, PropulsionPoint
from .simulation import PowerLimitError, SimulationParams, _solve_flight_point, simulate


def model(extrapolate=False):
    points = []
    for voltage in (8.1, 11.1):
        for thrust in (100, 200, 300, 400):
            eta = 6.6 * (thrust / 300) ** -0.38 * (voltage / 9) ** SHARED_VOLTAGE_EXPONENT
            points.append(PropulsionPoint(voltage, math.sqrt(thrust / 4e-6),
                                          thrust, thrust / eta, eta))
    return FittedPropulsionMap(PropulsionMap(points, allow_single_voltage_approximation=False),
                              allow_extrapolation=extrapolate)


def params(resistance=20):
    return SimulationParams(600, 200, 0, 4, 1, 5, resistance, 7.5, 1, 0.1, 1)


class FittedPropulsionTests(unittest.TestCase):
    def test_recover_known_law_and_rpm(self):
        fitted = model()
        self.assertAlmostEqual(fitted.a, 6.6)
        self.assertAlmostEqual(fitted.c, -0.38)
        self.assertAlmostEqual(fitted.rpm_coefficient, 4e-6)
        self.assertAlmostEqual(fitted.fit_mape_percent, 0)
        point = fitted.operate(9.5, 250)
        self.assertAlmostEqual(point.power_w * point.efficiency_g_per_w, 250)

    def test_explicitly_mark_and_bound_extrapolation(self):
        with self.assertRaises(MapLimitError):
            model().operate(12, 200)
        fitted = model(True)
        self.assertTrue(fitted.is_extrapolated(12, 200))
        self.assertFalse(fitted.is_extrapolated(9, 200))
        fitted.operate(12.6, 200)
        for voltage, thrust in ((12.7, 200), (7.4, 200), (9, 99), (9, 401)):
            with self.assertRaises(MapLimitError):
                fitted.operate(voltage, thrust)

    def test_known_voltage_is_recovered_with_manual_power_once(self):
        fitted = model()
        voltage, resistance, reference = 10.0, 0.02, 10.0
        total = 4 * fitted.operate(voltage, 200).power_w + 5
        baseline = voltage + resistance * (total / voltage - reference)
        curve = DischargeCurve([CurvePoint(0, baseline), CurvePoint(100, baseline - 1)], reference)
        actual, current, _, propulsion, power = _solve_flight_point(baseline, curve, fitted, params(), resistance)
        self.assertAlmostEqual(actual, voltage, places=9)
        self.assertAlmostEqual(power - propulsion, 5)
        self.assertAlmostEqual(current * actual, power)

    def test_zero_resistance_is_explicit(self):
        fitted = model()
        curve = DischargeCurve([CurvePoint(0, 10), CurvePoint(100, 9)], 10)
        actual, *_ = _solve_flight_point(10, curve, fitted, params(0), 0)
        self.assertEqual(actual, 10)

    def test_power_collapse_is_not_a_data_limit(self):
        fitted = model(True)
        curve = DischargeCurve([CurvePoint(0, 10), CurvePoint(100, 9)], 0.01)
        with self.assertRaises(PowerLimitError):
            _solve_flight_point(10, curve, fitted, params(1000), 1)

    def test_simulation_and_record_preserve_model_and_flags(self):
        fitted = model(True)
        curve = DischargeCurve([CurvePoint(0, 12), CurvePoint(100, 11.8)], 10)
        result = simulate(curve, fitted, params(0))
        self.assertTrue(all(p.model_extrapolated and p.rpm_is_estimate for p in result.points))
        self.assertAlmostEqual(result.used_energy_wh, result.propulsion_energy_wh + result.avionics_energy_wh)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            save_run_record(result, params(0), curve, fitted, path)
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(record["record_format_version"], 3)
        self.assertEqual(record["propulsion"]["fitted_model"]["b"], SHARED_VOLTAGE_EXPONENT)
        self.assertEqual(record["summary"]["extrapolated_sample_count"], len(result.points))


if __name__ == "__main__":
    unittest.main()
