"""数值与物理口径的回归检查；全部使用合成数据，不替代真实飞行验证。"""

import math
import unittest
from dataclasses import replace

from .battery_model import CurvePoint, DischargeCurve, create_demo_curve
from .propulsion_model import MapLimitError, PropulsionMap, PropulsionPoint, create_demo_propulsion_map
from .simulation import PowerLimitError, SimulationParams, simulate, _solve_flight_point


def curve(start=10, end=10, seconds=360):
    """10 A、360 s 对应 1 Ah 的受控测试曲线。"""
    return DischargeCurve([CurvePoint(0, start), CurvePoint(seconds, end)], 10)


def propulsion(voltages=(10,), powers=(25,)):
    """150 gf 目标推力时单桨功率恰好为给定值。"""
    return PropulsionMap([
        PropulsionPoint(v, rpm, thrust, power * thrust / 150, 150 / power)
        for v, power in zip(voltages, powers)
        for rpm, thrust in ((1000, 100), (2000, 200))
    ])


PARAMS = SimulationParams(400, 200, 0, 4, 1, 0, 0, 1, 1, 1, 1000)


class SimulationTests(unittest.TestCase):
    """检查联立求解、积分、终止事件和输入防护。"""

    def test_maximum_time_and_final_row_agree(self):
        for step in (1, 10):
            r = simulate(curve(), propulsion(), replace(PARAMS, max_time_s=2.5, time_step_s=step))
            self.assertEqual(r.duration_s, 2.5)
            self.assertEqual(r.points[-1].time_s, r.duration_s)
            self.assertEqual(r.termination_kind, "time")

    def test_constant_voltage_analytic_duration(self):
        r = simulate(curve(), propulsion(), PARAMS)
        self.assertAlmostEqual(r.duration_s, 360, places=8)
        self.assertEqual(r.points[-1].consumed_ah, 1)
        self.assertEqual(r.end_soc, 0)
        self.assertEqual(r.termination_kind, "capacity")
        self.assertAlmostEqual(r.average_current_a, 10)
        self.assertAlmostEqual(r.used_energy_wh, 10)

    def test_capacity_endpoint_uses_loaded_voltage(self):
        p = replace(PARAMS, equivalent_resistance_mohm=100)
        r = simulate(curve(), propulsion(powers=(20,)), p)
        expected = (11 + math.sqrt(121 - 32)) / 2
        self.assertAlmostEqual(r.end_voltage_v, expected)
        self.assertEqual(r.end_voltage_v, r.points[-1].voltage_v)
        self.assertGreater(r.end_voltage_v, 10)

    def test_partial_initial_soc_only_integrates_this_flight(self):
        r = simulate(curve(), propulsion(), replace(PARAMS, initial_soc=0.5))
        self.assertAlmostEqual(r.duration_s, 180)
        self.assertAlmostEqual(r.average_current_a, 10)
        self.assertAlmostEqual(r.used_energy_wh, 5)

    def test_charge_energy_and_average_are_time_weighted(self):
        r = simulate(curve(12, 9), propulsion(), replace(PARAMS, time_step_s=7, max_time_s=20))
        intervals = list(zip(r.points, r.points[1:]))
        ah = sum(a.current_a * (b.time_s - a.time_s) / 3600 for a, b in intervals)
        wh = sum(a.total_power_w * (b.time_s - a.time_s) / 3600 for a, b in intervals)
        self.assertAlmostEqual(r.consumed_ah, ah)
        self.assertAlmostEqual(r.used_energy_wh, wh)
        self.assertAlmostEqual(r.average_current_a, ah * 3600 / r.duration_s)
        for point in r.points:
            self.assertAlmostEqual(point.total_power_w, point.current_a * point.voltage_v)

    def test_cutoff_located_inside_step(self):
        r = simulate(curve(12, 8), propulsion(), replace(PARAMS, time_step_s=200, cutoff_voltage_v=11))
        self.assertAlmostEqual(r.duration_s, 108, places=6)
        self.assertAlmostEqual(r.end_voltage_v, 11, places=6)
        self.assertEqual(r.termination_kind, "limit")

    def test_current_limit_located_inside_step(self):
        r = simulate(curve(12, 8), propulsion(), replace(PARAMS, time_step_s=200, current_limit_a=100 / 11))
        self.assertAlmostEqual(r.duration_s, 108, places=6)
        self.assertIn("电流", r.cutoff_reason)

    def test_zero_duration_cutoff_is_valid_state(self):
        r = simulate(curve(), propulsion(), replace(PARAMS, cutoff_voltage_v=11))
        self.assertEqual(r.duration_s, 0)
        self.assertEqual(len(r.points), 1)
        self.assertEqual(r.used_energy_wh, 0)

    def test_map_boundary_is_not_battery_exhaustion(self):
        r = simulate(curve(12, 8), propulsion((11, 12), (25, 25)), replace(PARAMS, time_step_s=100))
        self.assertAlmostEqual(r.end_voltage_v, 11, places=6)
        self.assertEqual(r.termination_kind, "data")
        self.assertGreater(r.end_soc, 0.7)

    def test_voltage_solution_residual(self):
        data = propulsion((8, 12), (30, 25))
        p = replace(PARAMS, equivalent_resistance_mohm=200)
        v, i, _, _, power = _solve_flight_point(11, curve(), data, p, 0.2)
        self.assertAlmostEqual(v, 11 - 0.2 * (i - 10))
        self.assertAlmostEqual(power, v * i)

    def test_baseline_outside_map_but_loaded_solution_inside(self):
        p = replace(PARAMS, equivalent_resistance_mohm=100)
        v, *_ = _solve_flight_point(12.1, curve(), propulsion((10, 12), (40, 40)), p, 0.1)
        self.assertTrue(10 < v < 12)

    def test_no_power_solution_raises_instead_of_clamping(self):
        with self.assertRaises(PowerLimitError):
            _solve_flight_point(10, curve(), propulsion(powers=(100,)), PARAMS, 1)

    def test_euler_step_refinement_toward_analytic_duration(self):
        # P=100W、V(q)=12-4q：精确时长为 3600*积分Vdq/P=360s。
        errors = [abs(simulate(curve(12, 8), propulsion(), replace(PARAMS, time_step_s=dt)).duration_s - 360)
                  for dt in (2, 1, 0.5)]
        self.assertLess(errors[1], errors[0])
        self.assertLess(errors[2], errors[1])
        self.assertLess(errors[2], 0.2)

    def test_cancel(self):
        r = simulate(curve(), propulsion(), PARAMS, cancel=lambda: True)
        self.assertEqual(r.termination_kind, "cancelled")

    def test_propulsion_and_manual_avionics_are_two_distinct_branches(self):
        r = simulate(curve(), propulsion(), replace(PARAMS, avionics_power_w=10))
        for point in r.points:
            self.assertEqual(point.propulsion_power_w, 100)
            self.assertEqual(point.avionics_power_w, 10)  # 整机用电器不乘旋翼数量。
            self.assertEqual(point.total_power_w, 110)
            self.assertEqual(point.propulsion_current_a, 10)
            self.assertEqual(point.avionics_current_a, 1)
            self.assertAlmostEqual(point.current_a, point.propulsion_current_a + point.avionics_current_a)
        self.assertAlmostEqual(r.used_energy_wh, r.propulsion_energy_wh + r.avionics_energy_wh)
        self.assertAlmostEqual(r.avionics_energy_wh, 10 * r.duration_s / 3600)

    def test_large_step_does_not_skip_voltage_dip(self):
        data = DischargeCurve([CurvePoint(0, 12), CurvePoint(180, 8), CurvePoint(360, 12)], 10)
        r = simulate(data, propulsion(), replace(PARAMS, time_step_s=1000, cutoff_voltage_v=10))
        self.assertEqual(r.termination_kind, "limit")
        self.assertAlmostEqual(r.end_voltage_v, 10, places=6)
        self.assertLess(r.consumed_ah, 0.5)

    def test_large_step_does_not_skip_current_peak_at_map_layer(self):
        data = propulsion((8, 10, 12), (10, 25, 18))
        r = simulate(curve(12, 8), data, replace(PARAMS, time_step_s=1000, current_limit_a=9))
        self.assertEqual(r.termination_kind, "limit")
        self.assertAlmostEqual(r.points[-1].current_a, 9, places=6)
        self.assertGreater(r.end_voltage_v, 10)

    def test_invalid_parameters(self):
        for changes in ({"time_step_s": float("nan")}, {"rotor_count": 2.5},
                        {"airframe_mass_g": float("inf")}, {"current_limit_a": -1},
                        {"time_step_s": 1e-9}, {"initial_soc": 0}, {"thrust_to_weight": 0.9}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                simulate(curve(), propulsion(), replace(PARAMS, **changes))

    def test_original_demo_still_runs_with_consistent_efficiency(self):
        p = replace(PARAMS, battery_mass_g=230, avionics_power_w=5,
                    equivalent_resistance_mohm=20, cutoff_voltage_v=9, max_time_s=7200)
        r = simulate(create_demo_curve(), create_demo_propulsion_map(), p)
        self.assertGreater(r.duration_s, 0)
        for point in r.points:
            self.assertAlmostEqual(point.efficiency_g_per_w,
                                   point.total_thrust_g / point.propulsion_power_w)


class DataModelTests(unittest.TestCase):
    """数据入口不自动修饰、补造或外推不可辨认的实测值。"""

    def test_reject_bad_battery_data(self):
        for points in ([(0, 10), (0, 9)], [(1, 10), (0, 9)], [(0, float("nan")), (1, 10)]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                DischargeCurve([CurvePoint(*p) for p in points], 10)
        with self.assertRaises(ValueError):
            curve().voltage_at_consumed_ah(1.01)

    def test_time_offset_is_relative_to_first_sample(self):
        data = DischargeCurve([CurvePoint(100, 12), CurvePoint(460, 10)], 10)
        self.assertEqual(data.usable_capacity_ah, 1)
        self.assertEqual(data.voltage_at_consumed_ah(0.5), 11)

    def test_efficiency_is_derived_from_interpolated_power(self):
        point = create_demo_propulsion_map().operate(12, 157.5)
        self.assertAlmostEqual(point.efficiency_g_per_w, point.thrust_g / point.power_w)

    def test_exact_upper_voltage_does_not_require_lower_thrust_coverage(self):
        data = PropulsionMap([PropulsionPoint(v, t * 10, t, t / 5, 5)
                              for v, ts in ((10, (10, 20)), (12, (100, 200))) for t in ts])
        self.assertEqual(data.operate(12, 150).power_w, 30)
        with self.assertRaises(MapLimitError):
            data.operate(11, 150)

    def test_duplicate_nonmonotonic_and_inconsistent_propulsion(self):
        for points in ([PropulsionPoint(10, 1000, 100, 20, 5)] * 2,
                       [PropulsionPoint(10, 2000, 100, 20, 5), PropulsionPoint(10, 1000, 200, 40, 5)],
                       [PropulsionPoint(10, 1000, 100, 20, 10), PropulsionPoint(10, 2000, 200, 40, 5)]):
            with self.assertRaises(ValueError):
                PropulsionMap(points)


if __name__ == "__main__":
    unittest.main()
