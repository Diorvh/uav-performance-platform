"""界面编排：收集输入 → 后台计算 → 主线程呈现；模型和绘图分别位于独立文件。"""

from __future__ import annotations

import math
import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from platform_config import project_path, locate_database
from tkinter import filedialog, messagebox, ttk

from .battery_model import DischargeCurve
from .battery_dialog import BatteryDialog
from .battery_library import BatteryLibrary, DEFAULT_LIBRARY
from .cell_database import verify_cell_source
from .io_utils import load_curve_csv, load_propulsion_csv, save_result_csv, save_run_record
from .database_adapter import read_database
from .database_dialog import DatabaseDialog
from .plotting import TimeSeriesPanel
from .propulsion_model import PropulsionMap
from .example import FLIGHT_VALUES, load_example
from .plotting import DEFAULT_PLOTS
from .simulation import SimulationParams, SimulationResult, simulate


APP_TITLE = "悬停飞行模型仿真器"
POWER_BASES = {"动力净功率（已扣空载）": "net", "总电功率（需扣空载）": "electrical",
               "轴功率（换算后扣空载）": "shaft"}


class HoverSimulationApp(tk.Tk):
    """只有主线程可访问 Tk 控件；后台线程只处理冻结参数和模型数据。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1200x780")
        self.minsize(1000, 700)
        self.curve: DischargeCurve | None = None
        self.propulsion_map: PropulsionMap | None = None
        self.result: SimulationResult | None = None
        self.database_model = None
        self.battery_profile = None
        self.battery_provenance = None
        self._applying_battery = False
        self.field_widgets = {}
        self.run_params = None
        self.curve_path = self.propulsion_path = ""
        self.demo_curve = self.demo_propulsion = False
        self.busy = False
        self.cancel_event = threading.Event()
        self.messages = queue.Queue()
        self.poll_id = None
        self.worker = None
        self.editable_widgets = []
        self._variable_traces = []

        # 每个字段一个变量，名称沿用旧界面，便于对照参数与 SimulationParams。
        defaults = {
            "reference_current": "10", "map_voltage": "12", "drive_efficiency": "0.85",
            "no_load_current": "",
            "airframe_mass": "400", "battery_mass": "230", "payload_mass": "0",
            "rotor_count": "4", "thrust_to_weight": "1", "avionics_power": "5",
            "resistance": "0", "cutoff_voltage": "9", "initial_soc": "100",
            "time_step": "1", "current_limit": "0", "max_time": "120",
        }
        for name, value in defaults.items():
            setattr(self, name + "_var", tk.StringVar(master=self, value=value))
        self.power_basis_var = tk.StringVar(master=self, value=next(iter(POWER_BASES)))
        self.curve_info_var = tk.StringVar(master=self, value="尚未导入整包恒流曲线")
        self.propulsion_info_var = tk.StringVar(master=self, value="尚未导入单桨推进表")
        self.status_var = tk.StringVar(master=self, value="可载入数据库样例，或选择自己的电芯与推进数据。")
        self.result_context_var = tk.StringVar(master=self, value="尚无结果")
        self.warning_var = tk.StringVar(master=self, value="这是准稳态悬停模型，不是六自由度飞行动力学仿真。")
        self.summary_vars = {name: tk.StringVar(master=self, value="—") for name in (
            "duration", "reason", "start_rpm", "end_rpm", "end_voltage", "end_soc",
            "average_current", "peak_current", "propulsion_power", "mass_thrust", "energy",
            "avionics_power", "propulsion_energy", "avionics_energy")}
        self._build_ui()
        # 数据元信息改变后必须重导入，不能悄悄重解释旧测量。
        for name in defaults:
            source = "curve" if name == "reference_current" else (
                "map" if name in ("map_voltage", "drive_efficiency", "no_load_current") else None)
            variable = getattr(self, name + "_var")
            token = variable.trace_add("write", lambda *_args, s=source: self._input_changed(s))
            self._variable_traces.append((variable, token))
        token = self.power_basis_var.trace_add("write", lambda *_args: self._input_changed("map"))
        self._variable_traces.append((self.power_basis_var, token))
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build_ui(self) -> None:
        """异类控件自然排列；同语义参数用小表格对齐，避免全窗口强制网格。"""
        toolbar = ttk.Frame(self, padding=10)
        toolbar.pack(fill="x")
        for text, action in (("导入电池 CSV", self.import_curve), ("导入推进 CSV", self.import_propulsion_map),
                             ("读取推进数据库", self.import_database),
                             ("载入样例", self.load_demo_data), ("运行仿真", self.run_simulation)):
            button = ttk.Button(toolbar, text=text, command=action)
            button.pack(side="left", padx=(0, 8))
            self.editable_widgets.append(button)
        self.stop_button = ttk.Button(toolbar, text="停止", command=self.stop_simulation, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        self.export_button = ttk.Button(toolbar, text="导出结果 CSV", command=self.export_result, state="disabled")
        self.export_button.pack(side="left")
        self.record_button = ttk.Button(toolbar, text="导出追溯记录", command=self.export_record, state="disabled")
        self.record_button.pack(side="left", padx=(8, 0))
        status = ttk.Label(self, textvariable=self.status_var, padding=(10, 5), wraplength=950)
        status.pack(side="bottom", fill="x")
        status.bind("<Configure>", lambda e: status.configure(wraplength=max(100, e.width - 20)))

        body = ttk.Frame(self, padding=(10, 0, 10, 6))
        body.pack(fill="both", expand=True)
        self.controls = ttk.Notebook(body, width=340)
        self.controls.pack(side="left", fill="y", padx=(0, 12))
        data = ttk.Frame(self.controls, padding=8)
        flight = ttk.Frame(self.controls, padding=8)
        csv_settings = ttk.Frame(self.controls, padding=8)
        self.controls.add(data, text="1 · 测试数据")
        self.controls.add(flight, text="2 · 飞行参数")
        self.controls.add(csv_settings, text="CSV 口径")

        battery = ttk.LabelFrame(data, text="电池组 / 整包放电测试", padding=8)
        battery.pack(fill="x", pady=(0, 10))
        battery_buttons = ttk.Frame(battery)
        battery_buttons.pack(fill="x")
        for label, command in (("电池组模型库", self.open_battery_library),
                               ("改用整包 CSV", self.release_battery_profile)):
            button = ttk.Button(battery_buttons, text=label, command=command)
            button.pack(side="left", padx=(0, 8))
            self.editable_widgets.append(button)
        self._form(battery, [("整包参考恒流 / A", self.reference_current_var)])
        self._note(battery, self.curve_info_var)
        self._note(battery, "模型库：单电芯曲线按串并联换算。\n顶部电池 CSV 入口：直接读取整包测试。")
        prop = ttk.LabelFrame(data, text="单电机 / 单桨推进测试", padding=8)
        prop.pack(fill="x")
        self._note(prop, self.propulsion_info_var)
        self._note(prop, "动力：N × V × (I−I0)\n用电器：整机手动输入的恒功率\n总功率 = 动力净功率 + 用电器功率")
        self._note(data, "数据库按每组 I0 扣空载；CSV 的输入口径在“CSV 口径”页设置。\n扣除但实飞仍存在的耗电，应计入手动用电器功率；不会自动补回台架基线。")
        self._form(csv_settings, [("无电压列时测试电压 / V", self.map_voltage_var)])
        basis = ttk.Combobox(csv_settings, textvariable=self.power_basis_var, state="readonly",
                             values=tuple(POWER_BASES), width=27)
        basis.pack(anchor="w", pady=5)
        self.editable_widgets.append(basis)
        self._form(csv_settings, [("空载电流后备值 / A", self.no_load_current_var),
                                  ("轴功率换算总效率 / 0～1", self.drive_efficiency_var)])
        self._note(csv_settings, "净功率表：已经扣空载，不再次扣除。\n总电功率表：减 V×I0。\n轴功率表：先除以电机×电调效率，再减 V×I0。")
        self._note(csv_settings, "总电功率/轴功率表须提供 no_load_current_A 列或上方 I0 后备值（留空=未知）。不同电压 I0 不同时请使用逐行列。\n数据库入口不使用这些 CSV 转换设置。")
        self._note(csv_settings, "修改测试条件后需重新导入。只有单电压的 CSV 仍按电压无关近似，不能验证低压可达性。")

        for title, rows in (
            ("质量与等效悬停负载", [("机体裸重 / g", self.airframe_mass_var), ("电池质量 / g", self.battery_mass_var),
                                ("其他载荷 / g", self.payload_mass_var), ("旋翼数量", self.rotor_count_var),
                                ("等效推力系数（≥1）", self.thrust_to_weight_var)]),
            ("用电器与电池截止条件", [("整机用电器功率 / W", self.avionics_power_var), ("整包增量电阻 / mΩ", self.resistance_var),
                                    ("整包带载截止电压 / V", self.cutoff_voltage_var), ("初始窗口剩余容量 / %", self.initial_soc_var),
                                    ("整包电流限制 / A（0=关）", self.current_limit_var)]),
            ("积分设置", [("步长 / s", self.time_step_var), ("最大计算时间 / min", self.max_time_var)])):
            frame = ttk.LabelFrame(flight, text=title, padding=8)
            frame.pack(fill="x", pady=(0, 8))
            self._form(frame, rows)
        self._note(flight, "R=0 表示不做增量修正，不表示真实内阻为零。交流内阻不是已标定的直流增量电阻。\n纯悬停系数取 1；大于 1 仅为经验修正，不是爬升或机动仿真。")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        headline = ttk.Frame(right)
        headline.pack(fill="x", pady=(0, 4))
        ttk.Label(headline, text="计算到 / min：").pack(side="left")
        ttk.Label(headline, textvariable=self.summary_vars["duration"]).pack(side="left")
        ttk.Label(headline, textvariable=self.result_context_var, foreground="#92400e").pack(side="right")
        reason = ttk.Label(right, textvariable=self.summary_vars["reason"], foreground="#92400e", wraplength=560)
        reason.pack(fill="x", pady=(0, 5))
        reason.bind("<Configure>", lambda e: reason.configure(wraplength=max(100, e.width - 10)))
        self.result_tabs = ttk.Notebook(right)
        self.result_tabs.pack(fill="both", expand=True)
        self.chart_panel = TimeSeriesPanel(self.result_tabs, padding=5)
        details = ttk.Frame(self.result_tabs, padding=8)
        self.result_tabs.add(self.chart_panel, text="时序曲线")
        self.result_tabs.add(details, text="耗电分项与模型提示")
        self.plot = self.chart_panel.plot
        summary = ttk.LabelFrame(details, text="结果（数据边界 / 时间上限不等于真实续航）", padding=8)
        summary.pack(fill="x")
        for label, key in (("计算到 / min", "duration"), ("总质量 g / 等效总推力 gf", "mass_thrust"),
                           ("起始 → 终止转速 / rpm", "start_rpm"), ("终止端电压 / 窗口剩余容量", "end_voltage"),
                           ("平均 / 峰值总电流", "average_current"), ("起始 → 终止动力净功率", "propulsion_power"),
                           ("手动用电器功率", "avionics_power"),
                           ("动力净耗电", "propulsion_energy"), ("用电器耗电", "avionics_energy"),
                           ("本次用电量", "energy")):
            row = ttk.Frame(summary)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label).pack(side="left")
            ttk.Label(row, textvariable=self.summary_vars[key]).pack(side="right")
        ttk.Label(details, text="P总 = P动力净 + P用电器；I总 = P总 / V\n用电器是整机手动输入的恒功率，不乘旋翼数量。",
                  wraplength=560, justify="left").pack(fill="x", pady=10)
        warning = ttk.Label(details, textvariable=self.warning_var, foreground="#92400e", wraplength=560, justify="left")
        warning.pack(fill="x", pady=(6, 0))
        warning.bind("<Configure>", lambda e: warning.configure(wraplength=max(100, e.width - 10)))

    def _form(self, parent: tk.Misc, rows: list) -> None:
        """仅在同类键值表单内部使用 grid，不牵连其他区域的列宽。"""
        form = ttk.Frame(parent)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)
        for row, (label, variable) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(form, textvariable=variable, width=9)
            entry.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=3)
            self.editable_widgets.append(entry)
            self.field_widgets[str(variable)] = entry

    @staticmethod
    def _note(parent: tk.Misc, text: str | tk.StringVar) -> None:
        """提示按输入栏宽度换行。"""
        options = {"textvariable": text} if isinstance(text, tk.StringVar) else {"text": text}
        ttk.Label(parent, **options, wraplength=310, justify="left", foreground="#6b7280").pack(fill="x", pady=6)

    def _clear_result(self) -> None:
        """参数/数据修改、失败重跑后均不可导出旧结果。"""
        self.result = None
        self.run_params = None
        self.result_context_var.set("尚无结果")
        self.export_button.configure(state="disabled")
        self.record_button.configure(state="disabled")
        self.plot.set_result(None)
        for variable in self.summary_vars.values():
            variable.set("—")
        self.warning_var.set("单条恒流曲线是经验基线；精度需由真实悬停试验验证。")

    def _input_changed(self, source=None) -> None:
        if self._applying_battery:
            return
        self._clear_result()
        if source == "curve":
            self.battery_profile = self.battery_provenance = None
            self.curve, self.curve_path, self.demo_curve = None, "", False
            self.curve_info_var.set("测试条件已变，请重新导入电池曲线。")
            self._sync_battery_controls()
        if source == "map":
            self.database_model = None
            self.propulsion_map, self.propulsion_path, self.demo_propulsion = None, "", False
            self.propulsion_info_var.set("测试条件已变，请重新导入推进表。")
        self.status_var.set("输入已改变；请确认数据并重新运行。")

    @staticmethod
    def _float(variable: tk.StringVar) -> float:
        value = float(variable.get())
        if not math.isfinite(value):
            raise ValueError("输入必须是有限数字，不能是 NaN 或无穷大。")
        return value

    def _sync_battery_controls(self):
        """使用保存的电池组时，派生参数只展示，编辑统一回模型库。"""
        for variable in (self.reference_current_var, self.battery_mass_var, self.resistance_var, self.cutoff_voltage_var):
            widget = self.field_widgets.get(str(variable))
            if widget is not None:
                widget.configure(state="disabled" if self.busy or self.battery_profile is not None else "normal")

    def open_battery_library(self):
        if self.busy:
            return
        try:
            # 先读取，避免损坏的库导致留下半构造窗口。
            BatteryLibrary().load()
            BatteryDialog(self, self._use_battery_profile)
        except (ValueError, OSError) as exc:
            messagebox.showerror("模型库读取失败", str(exc), parent=self)

    def release_battery_profile(self):
        if self.busy:
            return
        self.battery_profile = self.battery_provenance = None
        self.curve, self.curve_path, self.demo_curve = None, "", False
        self._clear_result()
        self._sync_battery_controls()
        self.curve_info_var.set("整包 CSV 模式：先核对参考电流、重量、电阻和截止电压，再导入整包曲线。")
        self.status_var.set("已解除电池组关联，保存的模型仍保留在库中。")

    def _use_battery_profile(self, profile, curve, provenance):
        self._applying_battery = True
        try:
            self.reference_current_var.set(f"{curve.reference_current_a:.12g}")
            self.battery_mass_var.set(f"{profile.mass_g:.12g}")
            self.resistance_var.set(f"{profile.resistance_mohm:.12g}")
            self.cutoff_voltage_var.set(f"{profile.cutoff_v:.12g}")
        finally:
            self._applying_battery = False
        self.battery_profile, self.battery_provenance = profile, provenance
        self.curve, self.demo_curve = curve, False
        self.curve_path = provenance["cell_source"]["source"].get("path", "")
        self._clear_result()
        self._sync_battery_controls()
        basis = "整包实测" if profile.measured_mass_g is not None else "电芯＋附加重量估算"
        self.curve_info_var.set(f"{profile.name} · {profile.series}S{profile.parallel}P\n电芯：{profile.cell_name}\n"
                                f"整包 {profile.mass_g:.2f} g（{basis}）\n测试窗口 {curve.usable_capacity_ah:.3f} Ah\n"
                                "重量、电阻、截止电压由模型库带入；更改请进入模型库。")
        self.status_var.set("已选择电池组，重量计入飞机总重一次；使用保存的单电芯数据快照。")

    def _protected_paths(self):
        paths = [self.curve_path, self.propulsion_path, str(DEFAULT_LIBRARY)]
        if self.battery_provenance:
            paths.append(self.battery_provenance["library_path"])
            source = self.battery_provenance["cell_source"]["source"]
            paths.extend(source.get(key, "") for key in ("path", "database_path"))
        return tuple(p for p in paths if p)

    def import_curve(self) -> None:
        """导入失败保留先前有效数据，但不再展示旧计算结果。"""
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self, title="导入整包恒流放电曲线", filetypes=(("CSV", "*.csv"),))
        if not path:
            return
        self._clear_result()
        try:
            curve = load_curve_csv(path, self._float(self.reference_current_var))
        except (ValueError, OSError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self.curve, self.curve_path, self.demo_curve = curve, path, False
        self.battery_profile = self.battery_provenance = None
        self._sync_battery_controls()
        self.curve_info_var.set(f"{Path(path).name}\n{len(curve.points)} 点 · {curve.reference_current_a:g} A\n测试窗口 {curve.usable_capacity_ah:.3f} Ah")
        self.status_var.set("电池数据已导入。")

    def import_propulsion_map(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self, title="导入单电机/单桨推进表", filetypes=(("CSV", "*.csv"),))
        if not path:
            return
        self._clear_result()
        try:
            basis = POWER_BASES[self.power_basis_var.get()]
            idle = (self._float(self.no_load_current_var)
                    if basis != "net" and self.no_load_current_var.get().strip() else None)
            data = load_propulsion_csv(path, self._float(self.map_voltage_var),
                                       basis, self._float(self.drive_efficiency_var) if basis == "shaft" else 1,
                                       no_load_current_a=idle)
        except (ValueError, OSError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self.propulsion_map, self.propulsion_path, self.demo_propulsion = data, path, False
        self.database_model = None
        self.propulsion_info_var.set(data.describe(Path(path).name))
        self.status_var.set("推进数据已导入；请确认功率口径和所需推力覆盖范围。")

    def import_database(self) -> None:
        """数据库文件只读打开；曲线必须由用户明确选择。"""
        if self.busy:
            return
        default = project_path("force_database")
        path = filedialog.askopenfilename(parent=self, title="只读载入力效数据库",
                                         initialdir=str(default.parent), initialfile=default.name,
                                         filetypes=(("SQLite", "*.sqlite3 *.sqlite *.db"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            snapshot = read_database(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("读取失败", str(exc), parent=self)
            return
        DatabaseDialog(self, snapshot, self._use_database_model)

    def _use_database_model(self, selected) -> None:
        """动力只使用扣空载的净口径；用电器功率继续使用当前手动输入。"""
        self.power_basis_var.set(next(iter(POWER_BASES)))
        self._clear_result()
        self.database_model = selected
        self.propulsion_map = selected.propulsion_map
        self.propulsion_path = selected.provenance["database_path"]
        self.demo_propulsion = False
        orders = ",".join(format(g["display_order"], "03d") for g in selected.provenance["selected_groups"])
        self.propulsion_info_var.set(self.propulsion_map.describe(f"数据库组 {orders}"))
        self.status_var.set(f"已载入数据库组 {orders}；动力采用净功率，用电器手动另加。模型及外推范围见左侧说明。")

    def load_demo_data(self) -> None:
        """所有数据准备成功后才替换当前输入；不改写数据库或个人模型。"""
        if self.busy:
            return
        try:
            profile, curve, provenance, propulsion = load_example()
        except (OSError, ValueError) as exc:
            messagebox.showerror("样例加载失败", str(exc), parent=self)
            return
        for name, value in FLIGHT_VALUES.items():
            getattr(self, name + "_var").set(value)
        self._use_database_model(propulsion)
        self._use_battery_profile(profile, curve, provenance)
        self.chart_panel.mode_var.set("raw")
        self.chart_panel.select(DEFAULT_PLOTS)
        self.controls.select(1)
        self.result_tabs.select(0)
        self.curve_info_var.set(f"{profile.name} · 346 g\n电芯实验 97：约 {curve.usable_capacity_ah:.4f} Ah\n"
                                "单体增量电阻 0；整包线束 14 mΩ\n整包截止 7.5 V；样例不写入个人模型库。")
        self.status_var.set("已载入实测数据库样例和飞行参数；允许 7.5～12.6 V 外推（未验证），点击运行仿真。")

    def _read_params(self) -> SimulationParams:
        """只做界面单位换算；物理约束由 simulation 统一校验。"""
        f = self._float
        pack = self.battery_profile
        return SimulationParams(f(self.airframe_mass_var), pack.mass_g if pack else f(self.battery_mass_var), f(self.payload_mass_var),
                                f(self.rotor_count_var), f(self.thrust_to_weight_var), f(self.avionics_power_var),
                                pack.resistance_mohm if pack else f(self.resistance_var),
                                pack.cutoff_v if pack else f(self.cutoff_voltage_var), f(self.initial_soc_var) / 100,
                                f(self.time_step_var), f(self.max_time_var) * 60, f(self.current_limit_var))

    def _set_busy(self, busy: bool) -> None:
        """运行时锁住输入和导入，停止按钮仍可操作。"""
        self.busy = busy
        for widget in self.editable_widgets:
            widget.configure(state="disabled" if busy else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        self._sync_battery_controls()
        self.stop_button.configure(state="normal" if busy else "disabled")

    def run_simulation(self) -> None:
        if self.busy:
            return
        self._clear_result()
        try:
            if self.curve is None or self.propulsion_map is None:
                raise ValueError("请先载入两类有效数据；测试条件变更后需要重新导入。")
            if self.battery_provenance is not None:
                saved = self.battery_provenance
                verify_cell_source(saved['cell_source'])
                if saved.get("profile_kind") != "built_in_example":
                    packs, _ = BatteryLibrary(saved["library_path"]).load()
                    current = packs.get(saved["profile_id"])
                    if current is None or current[1] != saved["revision"] or current[0] != self.battery_profile:
                        self.release_battery_profile()
                        raise ValueError("电池组配置已变化，请从模型库重新选择。")
            if self.database_model is not None:
                provenance = self.database_model.provenance
                fresh = read_database(locate_database("force", provenance.get("database_id"), provenance["database_path"]))
                if fresh.sha256 != provenance["logical_snapshot_sha256"]:
                    self._input_changed("map")
                    raise ValueError("数据库内容或采用标记已改变，请重新读取数据库并选择曲线。")
            params = self._read_params()
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法计算", str(exc), parent=self)
            return
        self.cancel_event.clear()
        self.run_params = params
        self._set_busy(True)
        self.status_var.set("正在计算，可点击停止；输入已锁定。")
        self.worker = threading.Thread(target=self._compute, args=(self.messages, self.cancel_event, self.curve,
                                                                   self.propulsion_map, params), daemon=True)
        self.worker.start()
        self.poll_id = self.after(40, self._poll_result)

    @staticmethod
    def _compute(messages, cancel_event, curve, propulsion_map, params) -> None:
        """工作线程不访问 Tk；异常经队列回主线程，避免按钮永久处于忙碌状态。"""
        try:
            messages.put((simulate(curve, propulsion_map, params, cancel_event.is_set), None))
        except Exception as exc:
            if not isinstance(exc, ValueError):
                traceback.print_exc()
            messages.put((None, str(exc)))

    def _poll_result(self) -> None:
        self.poll_id = None
        try:
            result, error = self.messages.get_nowait()
        except queue.Empty:
            self.poll_id = self.after(40, self._poll_result)
            return
        self._set_busy(False)
        if error is not None:
            self.run_params = None
            self.status_var.set("计算失败；未保留旧结果。")
            messagebox.showerror("计算失败", error, parent=self)
            return
        self.result = result
        self._show_result(result)
        self.plot.set_result(result)
        self.result_tabs.select(self.chart_panel)
        self.export_button.configure(state="normal")
        self.record_button.configure(state="normal")
        self.status_var.set("计算结束。请先阅读停止原因和适用范围，再解读时长。")

    def stop_simulation(self) -> None:
        self.cancel_event.set()
        self.status_var.set("正在停止；将保留已计算的部分结果。")

    def _show_result(self, result: SimulationResult) -> None:
        """显式区分达到物理阈值、数据末端、时间上限和用户停止。"""
        values = {
            "duration": f"{result.duration_s / 60:.3f}", "reason": result.cutoff_reason,
            "mass_thrust": f"{result.total_mass_g:.1f} / {result.total_thrust_g:.1f}",
            "start_rpm": f"{result.start_rpm:.0f} → {result.end_rpm:.0f}",
            "end_rpm": f"{result.end_rpm:.0f}", "end_soc": f"{result.end_soc * 100:.1f}%",
            "end_voltage": f"{result.end_voltage_v:.3f} V / {result.end_soc * 100:.1f}%",
            "average_current": f"{result.average_current_a:.2f} / {result.peak_current_a:.2f} A",
            "peak_current": f"{result.peak_current_a:.2f} A",
            "propulsion_power": f"{result.start_propulsion_power_w:.1f} → {result.end_propulsion_power_w:.1f} W",
            "energy": f"{result.used_energy_wh:.3f} Wh",
            "avionics_power": f"{result.avionics_power_w:.2f} W",
            "propulsion_energy": f"{result.propulsion_energy_wh:.3f} Wh",
            "avionics_energy": f"{result.avionics_energy_wh:.3f} Wh",
        }
        if any(p.rpm_is_estimate for p in result.points):
            values["start_rpm"] += "（估算）"
        for key, value in values.items():
            self.summary_vars[key].set(value)
        notes = list(result.warnings)
        if self.battery_profile is not None:
            profile = self.battery_profile
            notes.insert(0, f"电池组 {profile.name}：{profile.series}S{profile.parallel}P，"
                         "按一致电芯、并联均流换算；容量为实测窗口，未建模电芯不一致和最弱单体提前截止。")
            source = self.battery_provenance['cell_source']['source']
            if source.get('partial_window'):
                notes.insert(0, '所选电芯实验截止未知或末端存在偏差：结果只覆盖已记录放电窗口，不代表完整电芯续航。')
            if source.get('approximate_current'):
                notes.insert(0, '所选实验含短时电流波动，按单参考电流近似；原始测点均保留，容量按实测电流积分。')
        self.result_context_var.set("含演示数据 · 不用于真实预测" if self.demo_curve or self.demo_propulsion else
                                    "含外推预测 · 未验证" if any(p.model_extrapolated for p in result.points) else
                                    "模型预测 · 请查看适用范围")
        if self.demo_curve or self.demo_propulsion:
            notes.insert(0, "含合成演示数据：结果不可用于真实续航判断。")
        if POWER_BASES[self.power_basis_var.get()] == "shaft":
            notes.append("轴功率按固定总效率换算；真实效率随工况变化，需实测验证。")
        self.warning_var.set("\n".join(notes))

    def export_result(self) -> None:
        if self.result is None or self.busy:
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出仿真时序（请另存，不覆盖测试数据）",
                                           defaultextension=".csv", filetypes=(("CSV", "*.csv"),),
                                           initialfile="演示结果.csv" if self.demo_curve or self.demo_propulsion else "悬停仿真结果.csv")
        if not path:
            return
        try:
            save_result_csv(self.result, path, self._protected_paths())
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出失败", f"{exc}\n若文件正被 Excel 占用，请手动关闭它或另存新文件。", parent=self)
            return
        self.status_var.set(f"已导出：{path}")

    def export_record(self) -> None:
        """追溯记录与时序分别保存，包含实际运行参数及数据库逻辑快照来源。"""
        if self.result is None or self.busy:
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出本次仿真追溯记录", defaultextension=".json",
                                           filetypes=(("JSON", "*.json"),), initialfile="悬停仿真追溯记录.json")
        if not path:
            return
        provenance = {
            "battery_source": ("saved cell snapshot" if self.battery_provenance else self.curve_path or "synthetic demo"),
            "battery_pack": self.battery_provenance,
            "propulsion_source": self.propulsion_path or "synthetic demo",
            "contains_demo_data": self.demo_curve or self.demo_propulsion,
            "database": self.database_model.provenance if self.database_model is not None else None,
        }
        try:
            save_run_record(self.result, self.run_params, self.curve, self.propulsion_map, path, provenance,
                            self._protected_paths())
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self.status_var.set(f"追溯记录已导出：{path}")

    def destroy(self) -> None:
        """先让工作线程退出再销毁 Tk；短等待后仍忙则通过主事件循环稍后关闭。"""
        self.cancel_event.set()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=0.1)
            if self.worker.is_alive():
                self.status_var.set("正在停止计算并关闭窗口…")
                self.after(40, self.destroy)
                return
        if self.poll_id is not None:
            self.after_cancel(self.poll_id)
        # 解除 trace→self→StringVar 的引用环。否则后台线程触发 GC 时，
        # 已关闭窗口的 Tk 变量可能在非主线程析构，引发 Tcl 线程错误。
        for variable, token in self._variable_traces:
            variable.trace_remove("write", token)
        self._variable_traces.clear()
        super().destroy()
        self.summary_vars.clear()
        for name, value in list(vars(self).items()):
            if isinstance(value, tk.Variable):
                setattr(self, name, None)


def main() -> None:
    """启动 GUI；也可不导入本文件，直接调用 simulation.simulate 批量计算。"""
    app = HoverSimulationApp()
    app.mainloop()
