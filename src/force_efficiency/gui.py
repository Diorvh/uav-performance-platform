from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .database import Database, group_name, shown
from .models import POINT_COLUMNS, X_FIELDS, Y_FIELDS

from platform_config import state_path, resolve_source_path

STATE_PATH = state_path("force_efficiency_gui_state.json")


class App:
    def __init__(self, root: tk.Tk, database: Database):
        self.root = root
        self.database = database
        self.state_path = STATE_PATH
        self.state = self.load_state()
        self.visible_groups = []
        self.search = tk.StringVar(value=self.state.get("search", ""))
        self.motor_filter = tk.StringVar(value=self.state.get("motor_filter", "全部"))
        self.propeller_filter = tk.StringVar(value=self.state.get("propeller_filter", "全部"))
        self.date_filter = tk.StringVar(value=self.state.get("date_filter", "全部"))
        self.voltage_filter = tk.StringVar(value=self.state.get("voltage_filter", "全部"))
        self.x_field = tk.StringVar(value=self.state.get("x_field", "油门 (μs)"))
        self.y_field = tk.StringVar(value=self.state.get("y_field", "力效 (g/W)"))
        self._voltage_values = {}
        self._restore_orders = {
            int(value) for value in self.state.get("selected_orders", []) if str(value).isdigit()
        }
        self.meta_vars = {key: tk.StringVar(value="—") for key in (
            "电机规格", "测试日期", "桨叶", "标称电压", "电调进角", "PWM 频率", "AM32 电机 kV", "空载电流", "重复组", "分析采用", "来源"
        )}

        root.title("电机桨叶力效数据库")
        try:
            root.geometry(str(self.state.get("geometry", "1380x840")))
        except tk.TclError:
            root.geometry("1380x840")
        root.minsize(1000, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)

        panes = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        left = ttk.Frame(panes, padding=6)
        right = ttk.Frame(panes, padding=6)
        panes.add(left, weight=1)
        panes.add(right, weight=4)
        self.build_left(left)
        self.build_right(right)
        self.reload_groups()

    def load_state(self) -> dict[str, object]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def build_left(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="数据组", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W)
        search_row = ttk.Frame(parent)
        search_row.pack(fill=tk.X, pady=(8, 6))
        entry = ttk.Entry(search_row, textvariable=self.search)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _event: self.reload_groups())
        ttk.Button(search_row, text="筛选", command=self.reload_groups).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(search_row, text="清除筛选", command=self.clear_filters).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(parent, text="Ctrl/Shift 可选择多组；重复项显示 R 编号").pack(anchor=tk.W, pady=(0, 4))

        all_groups = self.database.groups()
        motor_values = ["全部", *sorted({row["motor_spec"] for row in all_groups if row["motor_spec"]})]
        propeller_values = ["全部", "未标注", *sorted({row["propeller"] for row in all_groups if row["propeller"]})]
        date_values = ["全部", "未标注", *sorted({row["test_date"] for row in all_groups if row["test_date"]})]
        voltage_numbers = sorted({row["nominal_voltage_v"] for row in all_groups if row["nominal_voltage_v"] is not None})
        self._voltage_values = {f"{value:g} V": value for value in voltage_numbers}
        voltage_values = ["全部", *self._voltage_values]
        filter_frame = ttk.LabelFrame(parent, text="条件筛选", padding=6)
        filter_frame.pack(fill=tk.X, pady=(0, 6))
        for label, variable, values in (
            ("电机", self.motor_filter, motor_values),
            ("桨叶", self.propeller_filter, propeller_values),
            ("测试日期", self.date_filter, date_values),
            ("标称电压", self.voltage_filter, voltage_values),
        ):
            ttk.Label(filter_frame, text=label).pack(anchor=tk.W)
            if variable.get() not in values:
                variable.set("全部")
            combo = ttk.Combobox(filter_frame, textvariable=variable, values=values, state="readonly")
            combo.pack(fill=tk.X, pady=(0, 4))
            combo.bind("<<ComboboxSelected>>", lambda _event: self.reload_groups())

        self.repeat_hint = ttk.Label(parent, foreground="#9c6500")
        self.repeat_hint.pack(anchor=tk.W, pady=(0, 4))

        list_frame = ttk.Frame(parent)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.group_list = tk.Listbox(list_frame, selectmode=tk.EXTENDED, exportselection=False)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.group_list.yview)
        self.group_list.configure(yscrollcommand=scrollbar.set)
        self.group_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.group_list.bind("<<ListboxSelect>>", lambda _event: self.refresh_selection())

    def build_right(self, parent: ttk.Frame) -> None:
        from matplotlib import rcParams
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        details = ttk.LabelFrame(parent, text="数据组设置", padding=8)
        details.pack(fill=tk.X)
        for index, (label, variable) in enumerate(self.meta_vars.items()):
            row, column = divmod(index, 4)
            cell = ttk.Frame(details)
            cell.grid(row=row, column=column, sticky="ew", padx=6, pady=3)
            ttk.Label(cell, text=label).pack(anchor=tk.W)
            ttk.Label(cell, textvariable=variable, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor=tk.W)
            details.columnconfigure(column, weight=1)

        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X, pady=8)
        ttk.Label(controls, text="横轴").pack(side=tk.LEFT)
        ttk.Combobox(
            controls,
            textvariable=self.x_field,
            values=list(X_FIELDS),
            state="readonly",
            width=16,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(controls, text="纵轴").pack(side=tk.LEFT)
        ttk.Combobox(
            controls,
            textvariable=self.y_field,
            values=list(Y_FIELDS),
            state="readonly",
            width=16,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="标记为采用", command=lambda: self.mark_selected(True)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(controls, text="取消标记", command=lambda: self.mark_selected(False)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(controls, text="重绘", command=self.refresh_selection).pack(side=tk.LEFT)
        self.status = ttk.Label(controls)
        self.status.pack(side=tk.RIGHT)

        vertical = ttk.Panedwindow(parent, orient=tk.VERTICAL)
        vertical.pack(fill=tk.BOTH, expand=True)
        plot_frame = ttk.Frame(vertical)
        table_frame = ttk.Frame(vertical)
        vertical.add(plot_frame, weight=3)
        vertical.add(table_frame, weight=2)

        rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        rcParams["axes.unicode_minus"] = False
        self.figure = Figure(figsize=(8, 4), dpi=100)
        self.axes = self.figure.add_subplot()
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        toolbar = NavigationToolbar2Tk(self.canvas, plot_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        column_ids = [column[0] for column in POINT_COLUMNS]
        self.table = ttk.Treeview(table_frame, columns=column_ids, show="headings")
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.table.xview)
        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        self.table.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        for column_id, title, width in POINT_COLUMNS:
            self.table.heading(column_id, text=title)
            self.table.column(column_id, width=width, anchor=tk.E if column_id != "group_name" else tk.W)
        self.table.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

    def reload_groups(self) -> None:
        self.visible_groups = [
            row for row in self.database.groups(self.search.get()) if self.matches_filters(row)
        ]
        all_rows = self.database.groups()
        self._marked_repeat_sets = {
            row["repeat_set"] for row in all_rows
            if row["repeat_set"] is not None and row["analysis_preferred"]
        }
        pending_sets = sorted({
            row["repeat_set"] for row in all_rows
            if row["repeat_set"] is not None and row["repeat_set"] not in self._marked_repeat_sets
        })
        self.group_list.delete(0, tk.END)
        for row in self.visible_groups:
            index = self.group_list.size()
            self.group_list.insert(tk.END, self.list_name(row))
            if row["repeat_set"] is not None:
                if row["analysis_preferred"]:
                    colors = ("#e2f0d9", "#38761d")
                elif row["repeat_set"] in self._marked_repeat_sets:
                    colors = ("#f2f2f2", "#666666")
                else:
                    colors = ("#fff2cc", "#9c6500")
                self.group_list.itemconfig(index, background=colors[0], foreground=colors[1])
        if pending_sets:
            self.repeat_hint.configure(
                text="待标记重复组：" + "、".join(f"R{repeat_set:03d}" for repeat_set in pending_sets)
            )
        else:
            self.repeat_hint.configure(text="重复组均已完成标记")
        suffix = f"；待标记重复组 {len(pending_sets)} 组" if pending_sets else ""
        self.status.configure(text=f"{len(self.visible_groups)} 组{suffix}")
        restore_orders = self._restore_orders
        self._restore_orders = set()
        for index, row in enumerate(self.visible_groups):
            if row["display_order"] in restore_orders:
                self.group_list.selection_set(index)
        if self.group_list.curselection():
            self.refresh_selection()
        else:
            self.clear_view("请选择左侧数据组")

    def list_name(self, row) -> str:
        name = group_name(row)
        if row["repeat_set"] is None:
            return name
        state = (
            "已采用" if row["analysis_preferred"]
            else "未采用" if row["repeat_set"] in self._marked_repeat_sets
            else "待标记"
        )
        return f"[{state} R{row['repeat_set']:03d}] {name}"

    def clear_filters(self) -> None:
        self.search.set("")
        self.motor_filter.set("全部")
        self.propeller_filter.set("全部")
        self.date_filter.set("全部")
        self.voltage_filter.set("全部")
        self.reload_groups()

    def matches_filters(self, row) -> bool:
        if self.motor_filter.get() != "全部" and row["motor_spec"] != self.motor_filter.get():
            return False
        propeller = self.propeller_filter.get()
        if propeller == "未标注" and row["propeller"] not in (None, ""):
            return False
        if propeller not in ("全部", "未标注") and row["propeller"] != propeller:
            return False
        test_date = self.date_filter.get()
        if test_date == "未标注" and row["test_date"] not in (None, ""):
            return False
        if test_date not in ("全部", "未标注") and row["test_date"] != test_date:
            return False
        voltage = self.voltage_filter.get()
        return voltage == "全部" or row["nominal_voltage_v"] == self._voltage_values.get(voltage)

    def selected_groups(self):
        return [self.visible_groups[index] for index in self.group_list.curselection()]

    def mark_selected(self, preferred: bool) -> None:
        groups = self.selected_groups()
        if not groups:
            self.status.configure(text="请先选择要标记的数据组")
            return

        if preferred:
            selected_by_repeat = {}
            for group in groups:
                repeat_set = group["repeat_set"]
                if repeat_set is not None:
                    selected_by_repeat.setdefault(repeat_set, []).append(group)
            conflicts = [repeat_set for repeat_set, rows in selected_by_repeat.items() if len(rows) > 1]
            if conflicts:
                sets = "、".join(f"R{repeat_set:03d}" for repeat_set in conflicts)
                self.status.configure(text=f"同一重复组只能选择一条：{sets}")
                return

        for group in groups:
            self.database.set_analysis_preferred(group["id"], preferred)
        self._restore_orders = {group["display_order"] for group in groups}
        self.reload_groups()
        action = "标记为采用" if preferred else "取消标记"
        self.status.configure(text=f"已保存：{action} {len(groups)} 组")

    def refresh_selection(self) -> None:
        groups = self.selected_groups()
        if not groups:
            self.clear_view("请选择左侧数据组")
            return

        self.show_metadata(groups)
        points = self.database.points([row["id"] for row in groups])
        self.show_table(points, {row["id"]: group_name(row) for row in groups})
        self.show_plot(groups, points)
        self.status.configure(text=f"已选 {len(groups)} 组，{len(points)} 个点")

    def show_metadata(self, groups) -> None:
        if len(groups) > 1:
            for variable in self.meta_vars.values():
                variable.set("多组对比")
            return
        row = groups[0]
        values = {
            "电机规格": row["motor_spec"],
            "测试日期": row["test_date"],
            "桨叶": row["propeller"],
            "标称电压": None if row["nominal_voltage_v"] is None else f'{row["nominal_voltage_v"]:g} V',
            "电调进角": None if row["esc_advance_deg"] is None else f'{row["esc_advance_deg"]:g}°',
            "PWM 频率": None if row["pwm_frequency_khz"] is None else f'{row["pwm_frequency_khz"]:g} kHz',
            "AM32 电机 kV": row["am32_motor_kv"],
            "空载电流": None if row["no_load_current_a"] is None else f'{row["no_load_current_a"]:g} A',
            "重复组": f'R{row["repeat_set"]:03d}' if row["repeat_set"] is not None else "无重复",
            "分析采用": (
                "已标记采用" if row["analysis_preferred"]
                else "未采用（同组已选）" if row["repeat_set"] in self._marked_repeat_sets
                else "待手动标记" if row["repeat_set"] is not None
                else "自动采用（无重复）"
            ),
            "来源": f'{resolve_source_path(row["source_file"], "force")} / {row["source_sheet"]} / {row["source_anchor"]}',
        }
        for key, value in values.items():
            self.meta_vars[key].set(shown(value))

    def show_table(self, points, names) -> None:
        self.table.delete(*self.table.get_children())
        for row in points:
            self.table.insert("", tk.END, values=(
                names[row["group_id"]],
                shown(row["throttle_us"]), shown(row["voltage_v"]), shown(row["current_a"]),
                shown(row["rpm"]), shown(row["thrust_mn"]), shown(row["power_w"]),
                shown(row["thrust_g"]), shown(row["efficiency_g_per_w"]),
            ))

    def show_plot(self, groups, points) -> None:
        self.axes.clear()
        self.axes.set_axis_on()
        x_column, x_label = X_FIELDS[self.x_field.get()]
        y_column, y_label = Y_FIELDS[self.y_field.get()]
        for group in groups:
            rows = [
                row for row in points
                if row["group_id"] == group["id"]
                and row[x_column] is not None
                and row[y_column] is not None
            ]
            if rows:
                self.axes.plot(
                    [row[x_column] for row in rows],
                    [row[y_column] for row in rows],
                    marker="o",
                    label=group_name(group),
                )
        self.axes.set_xlabel(x_label)
        self.axes.set_ylabel(y_label)
        self.axes.grid(True, alpha=0.25)
        if self.axes.lines:
            self.axes.legend(fontsize=8)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def clear_view(self, message: str) -> None:
        for variable in self.meta_vars.values():
            variable.set("—")
        self.table.delete(*self.table.get_children())
        self.axes.clear()
        self.axes.text(0.5, 0.5, message, ha="center", va="center", transform=self.axes.transAxes)
        self.axes.set_axis_off()
        self.canvas.draw_idle()

    def close(self) -> None:
        state = {
            "geometry": self.root.geometry(),
            "search": self.search.get(),
            "motor_filter": self.motor_filter.get(),
            "propeller_filter": self.propeller_filter.get(),
            "date_filter": self.date_filter.get(),
            "voltage_filter": self.voltage_filter.get(),
            "x_field": self.x_field.get(),
            "y_field": self.y_field.get(),
            "selected_orders": [
                self.visible_groups[index]["display_order"] for index in self.group_list.curselection()
            ],
        }
        try:
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        self.database.close()
        self.root.destroy()
