"""本程序的 Tk 交互与布局检查；窗口全透明，弹窗用替身，不操作其他应用。"""

import time
import tempfile
import gc
import unittest
from pathlib import Path
from unittest.mock import patch

from .gui import HoverSimulationApp, POWER_BASES
from .battery_model import create_demo_curve
from .propulsion_model import create_demo_propulsion_map
from .plotting import PLOTS, DEFAULT_PLOTS
from .database_adapter import read_database
from .database_dialog import DatabaseDialog
from .test_database import make_database, add_group
from .test_simulation import curve


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.app = HoverSimulationApp()
        self.app.attributes("-alpha", 0)
        self.addCleanup(self.close_app)
        self.dialog = patch("simulation.gui.messagebox.showerror").start()
        self.addCleanup(patch.stopall)
        self.app.update()

    def close_app(self):
        """测试会创建多个 Tk 解释器；必须在主线程释放上一个，再启动下一后台任务。"""
        app, self.app = self.app, None
        app.destroy()
        # 生产程序只用一个 Tk；此处也让测试框架的跨测试引用环在主线程回收。
        del app
        gc.collect()

    def finish(self):
        """让 Tk 自己处理工作线程回执；超过五秒视为卡住而非无限等待。"""
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.005)
        self.assertFalse(self.app.busy, "后台计算未按时释放界面")

    def load_synthetic_fixture(self):
        """交互用例使用独立合成夹具，不依赖 Releases 数据库或个人模型。"""
        self.app.battery_profile = self.app.battery_provenance = None
        self.app.reference_current_var.set("10")
        self.app.map_voltage_var.set("12")
        self.app.power_basis_var.set(next(iter(POWER_BASES)))
        self.app.curve = create_demo_curve()
        self.app.propulsion_map = create_demo_propulsion_map()
        self.app.database_model = None
        self.app.curve_path = self.app.propulsion_path = ""
        self.app.demo_curve = self.app.demo_propulsion = True
        self.app._clear_result()
        self.app._sync_battery_controls()

    def run_demo(self):
        self.load_synthetic_fixture()
        self.app.run_simulation()
        self.assertTrue(self.app.busy)
        self.finish()
        self.assertIsNotNone(self.app.result)

    def test_demo_background_completion_and_all_plots(self):
        self.run_demo()
        self.assertIn("演示", self.app.warning_var.get())
        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        for name in PLOTS:
            self.app.chart_panel.select((name,))
            self.assertGreater(len(self.app.plot.find_withtag("series")), 0)
        self.dialog.assert_not_called()

    def test_multiseries_modes_keep_result_and_handle_zero_initial_value(self):
        self.run_demo()
        result = self.app.result
        self.app.chart_panel.select(DEFAULT_PLOTS)
        self.assertEqual(len(self.app.plot.find_withtag("series")), 4)
        self.app.chart_panel.mode_var.set("relative")
        self.app.chart_panel.refresh_selection()
        self.assertEqual(len(self.app.plot.find_withtag("series")), 4)
        self.assertIs(self.app.result, result)
        self.app.avionics_power_var.set("0")
        self.app.run_simulation()
        self.finish()
        self.app.chart_panel.select(("用电器功率",))
        self.assertEqual(len(self.app.plot.find_withtag("series")), 0)
        self.app.chart_panel.mode_var.set("raw")
        self.app.chart_panel.refresh_selection()
        self.assertEqual(len(self.app.plot.find_withtag("series")), 1)

    def test_cursor_reads_original_units_and_selection_can_be_empty(self):
        from types import SimpleNamespace
        self.run_demo()
        self.app.chart_panel.select(DEFAULT_PLOTS)
        left, right, top, bottom = self.app.plot._axes[0]
        self.app.plot._inspect(SimpleNamespace(x=(left + right) / 2, y=(top + bottom) / 2))
        reading = self.app.chart_panel.inspection.get("1.0", "end")
        self.assertIn("rpm", reading)
        self.assertIn("gf/W", reading)
        self.assertIn("电池总电流", reading)
        self.app.chart_panel.select(())
        self.assertEqual(len(self.app.plot.find_withtag("series")), 0)

    def test_input_edit_invalidates_result_not_data(self):
        self.run_demo()
        self.app.airframe_mass_var.set("500")
        self.assertIsNone(self.app.result)
        self.assertIsNone(self.app.plot.result)
        self.assertIsNotNone(self.app.curve)
        self.assertEqual(str(self.app.export_button.cget("state")), "disabled")

    def test_metadata_edit_requires_reimport(self):
        self.load_synthetic_fixture()
        self.app.reference_current_var.set("12")
        self.assertIsNone(self.app.curve)
        self.assertIsNotNone(self.app.propulsion_map)
        self.app.map_voltage_var.set("11")
        self.assertIsNone(self.app.propulsion_map)
        self.load_synthetic_fixture()
        self.app.power_basis_var.set("轴功率（换算后扣空载）")
        self.assertIsNone(self.app.propulsion_map)

    def test_fractional_rotor_fails_without_stale_result(self):
        self.run_demo()
        self.app.rotor_count_var.set("2.5")
        self.app.run_simulation()
        self.finish()
        self.assertIsNone(self.app.result)
        self.dialog.assert_called_once()
        self.assertEqual(str(self.app.stop_button.cget("state")), "disabled")

    def test_user_cancellation_returns_partial_result(self):
        self.load_synthetic_fixture()
        self.app.time_step_var.set("0.0001")
        self.app.max_time_var.set("0.3")
        self.app.run_simulation()
        self.app.stop_simulation()
        self.finish()
        self.assertEqual(self.app.result.termination_kind, "cancelled")

    def test_missing_data_does_not_start_worker(self):
        self.app.run_simulation()
        self.assertFalse(self.app.busy)
        self.dialog.assert_called_once()

    def test_destroy_releases_tk_variables_on_main_thread(self):
        # 使用另一窗口，主测试窗口仍由统一清理函数管理。
        extra = HoverSimulationApp()
        extra.attributes("-alpha", 0)
        extra.destroy()
        self.assertEqual(extra._variable_traces, [])
        self.assertEqual(extra.summary_vars, {})
        self.assertIsNone(extra.reference_current_var)
        del extra
        gc.collect()

    def test_database_selection_and_changed_marks_invalidate_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            make_database(path)
            add_group(path, 99, 76, voltage=8)
            add_group(path, 100, 77, voltage=12)
            chooser = DatabaseDialog(self.app, read_database(path), self.app._use_database_model)
            chooser.attributes("-alpha", 0)
            # This fixture has two points per voltage: choose its supported model.
            chooser.model_var.set("实测分层插值（对照）")
            chooser.tree.selection_set("99", "100")
            chooser.apply_selection()
            self.assertIsNotNone(self.app.database_model)
            self.app.curve = curve(11, 9)
            self.app.battery_mass_var.set("200")
            self.app.cutoff_voltage_var.set("8")
            self.app.run_simulation()
            self.finish()
            self.assertIsNotNone(self.app.result)
            self.app.geometry("1000x700")
            self.app.update()
            self.assertGreaterEqual(self.app.plot.winfo_height(), 140)
            self.dialog.assert_not_called()
            # 只修改临时库：代表数据库端发生一次人工标记/导入更新。
            add_group(path, 101, 78, voltage=7.5, count=1)
            self.app.run_simulation()
            self.assertIsNone(self.app.result)
            self.assertIsNone(self.app.database_model)
            self.dialog.assert_called_once()

    def test_layout_widgets_fit_at_supported_window_sizes(self):
        self.run_demo()
        for geometry in ("1000x700", "1200x780", "1360x860"):
            self.app.geometry(geometry)
            self.app.update()
            for tab in self.app.controls.tabs():
                self.app.controls.select(tab)
                self.app.update()
                parent = self.app.nametowidget(tab)
                for child in parent.winfo_children():
                    with self.subTest(size=geometry, widget=str(child)):
                        self.assertLessEqual(child.winfo_y() + child.winfo_height(), parent.winfo_height())
            self.assertGreaterEqual(self.app.plot.winfo_height(), 140)
            self.assertGreaterEqual(self.app.plot.winfo_width(), 180)
            self.assertLessEqual(self.app.record_button.winfo_rootx() + self.app.record_button.winfo_width(),
                                 self.app.winfo_rootx() + self.app.winfo_width())


if __name__ == "__main__":
    unittest.main()
