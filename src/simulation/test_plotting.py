"""图表数据变换检查：原值与归一化不能混淆，显示抽样不得丢失尖峰。"""

import unittest

from .plotting import display_indices, relative_to_initial


class PlotDataTests(unittest.TestCase):
    def test_normalization_is_relative_to_initial_not_shared_physical_axis(self):
        self.assertEqual(relative_to_initial([12, 9, 6]), [100, 75, 50])
        self.assertEqual(relative_to_initial([5000, 6000]), [100, 120])

    def test_zero_initial_value_has_no_relative_percentage(self):
        self.assertIsNone(relative_to_initial([0, 0]))
        self.assertIsNone(relative_to_initial([0, 1]))

    def test_display_reduction_preserves_endpoints_and_spike(self):
        values = [1.0] * 10000
        values[1234] = 200
        values[4567] = -20
        selected = display_indices(values, 100)
        self.assertTrue({0, 1234, 4567, 9999}.issubset(selected))
        self.assertLessEqual(len(selected), 202)
        self.assertEqual(display_indices([5], 100), [0])


if __name__ == "__main__":
    unittest.main()
