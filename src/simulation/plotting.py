"""共享时间轴的多参数图：真实单位分图 / 相对初值叠加，不参与物理计算。"""

from __future__ import annotations

import bisect
import math
import tkinter as tk
from tkinter import ttk

from .simulation import SimulationResult


# 显示名 → 时序字段、原始单位、显示倍率；功率/电流明确区分两个耗电支路。
PLOTS = {
    "电池端电压": ("voltage_v", "V", 1),
    "单电机转速": ("rpm", "rpm", 1),
    "电池总电流": ("current_a", "A", 1),
    "动力净力效": ("efficiency_g_per_w", "gf/W", 1),
    "动力净电流": ("propulsion_current_a", "A", 1),
    "用电器电流": ("avionics_current_a", "A", 1),
    "动力净功率": ("propulsion_power_w", "W", 1),
    "用电器功率": ("avionics_power_w", "W", 1),
    "总功率": ("total_power_w", "W", 1),
    "窗口剩余容量": ("soc", "%", 100),
    "参考放电基线": ("baseline_voltage_v", "V", 1),
}
DEFAULT_PLOTS = tuple(PLOTS)[:4]
COLORS = dict(zip(PLOTS, ("#2563eb", "#d97706", "#dc2626", "#059669", "#be185d",
                         "#7c3aed", "#0e7490", "#64748b", "#92400e", "#4338ca", "#475569")))


def relative_to_initial(values: list[float]) -> list[float] | None:
    """初值=100%；初值为0时相对值无定义，不伪造一条百分比曲线。"""
    if not values or values[0] == 0:
        return None
    relative = [value / values[0] * 100 for value in values]
    return relative if all(math.isfinite(v) for v in relative) else None


def display_indices(values: list[float], width_px: int) -> list[int]:
    """只压缩显示：保留首末点与每桶极值，不改变原始时序和导出数据。"""
    if not values:
        return []
    stride = max(1, math.ceil(len(values) / max(1, width_px)))
    indices = {0, len(values) - 1}
    for start in range(0, len(values), stride):
        bucket = range(start, min(start + stride, len(values)))
        indices.add(min(bucket, key=values.__getitem__))
        indices.add(max(bucket, key=values.__getitem__))
    return sorted(indices)


class PlotCanvas(tk.Canvas):
    """所有曲线共用完整时长；分图有独立真实单位，叠加图使用相对初值。"""

    def __init__(self, master: tk.Misc, **kwargs):
        super().__init__(master, background="white", highlightthickness=0, yscrollincrement=30, **kwargs)
        self.result: SimulationResult | None = None
        self.selected_names = DEFAULT_PLOTS
        self.mode = "raw"
        self.on_inspect = None
        self._times = ()
        self._values = {}
        self._axes = []
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Motion>", self._inspect)
        self.bind("<Leave>", lambda _event: self.delete("cursor"))
        self.bind("<MouseWheel>", self._scroll)

    def set_result(self, result: SimulationResult | None) -> None:
        """结果失效时同时清缓存和读数，不能显示旧输入的曲线。"""
        self.result = result
        self._times = tuple(p.time_s for p in result.points) if result else ()
        self._values.clear()
        self.yview_moveto(0)
        if self.on_inspect:
            self.on_inspect("移动鼠标到曲线区，查看最近时刻的原始数值。" if result else "尚无仿真结果。")
        self.redraw()

    def configure_series(self, names, mode="raw") -> None:
        """修改显示选择不改变模型输入，不触发重新仿真。"""
        self.selected_names = tuple(name for name in PLOTS if name in names)
        self.mode = mode
        self.yview_moveto(0)
        self.redraw()

    def _series(self, name):
        if name not in self._values:
            field, _unit, scale = PLOTS[name]
            self._values[name] = [getattr(point, field) * scale for point in self.result.points]
        return self._values[name]

    def _label(self, name):
        if name == "单电机转速" and self.result and self.result.points[0].rpm_is_estimate:
            return name + "（估算）"
        return name

    def _scroll(self, event):
        if event.delta:
            self.yview_scroll(-1 if event.delta > 0 else 1, "units")
            self.delete("cursor")
        return "break"

    def _draw_axes(self, y0, height, width, low, high, title, unit):
        """每个分图的 x 映射完全一致；标题和 y 刻度不会与曲线混用单位。"""
        left, right, top, bottom = 76, width - 24, y0 + 25, y0 + height - 40
        self.create_text(10, y0 + 10, text=f"{title} / {unit}", anchor="w", fill="#1f2937", tags="axis")
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * (bottom - top)
            self.create_line(left, y, right, y, fill="#e5e7eb", tags="axis")
            self.create_text(left - 7, y, text=f"{low + fraction * (high - low):.4g}", anchor="e", tags="axis")
        for fraction in (0, 0.25, 0.5, 0.75, 1):
            x = left + fraction * (right - left)
            self.create_text(x, bottom + 14, text=f"{fraction * self._time_max / 60:.3g}", tags="axis")
        self.create_line(left, top, left, bottom, right, bottom, fill="#9ca3af", tags="axis")
        self.create_text((left + right) / 2, y0 + height - 8, text="时间 / min", fill="#6b7280", tags="axis")
        self._axes.append((left, right, top, bottom))
        return left, right, top, bottom

    @staticmethod
    def _range(values, soc=False):
        if soc:
            return 0, 100
        low, high = min(values), max(values)
        margin = max((high - low) * 0.08, abs(high) * 0.01, 0.01)
        return low - margin, high + margin

    def _draw_line(self, name, values, axes, low, high):
        left, right, top, bottom = axes
        coords = []
        for i in display_indices(values, int(right - left)):
            coords.extend((left + self._times[i] / self._time_max * (right - left),
                           bottom - (values[i] - low) / (high - low) * (bottom - top)))
        if len(coords) >= 4:
            self.create_line(*coords, fill=COLORS[name], width=2, tags="series")
        elif coords:
            x, y = coords
            self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=COLORS[name], outline="", tags="series")

    def redraw(self) -> None:
        """重绘显示区；很多分图时保留最小图高并滚动，不将坐标挤到不可读。"""
        self.delete("all")
        self._axes.clear()
        width, height = self.winfo_width(), self.winfo_height()
        self.configure(scrollregion=(0, 0, width, height))
        if width < 220 or height < 140:
            return
        if self.result is None or not self.selected_names:
            text = "导入数据并运行仿真，曲线会显示在这里。" if self.result is None else "请勾选至少一个显示参数。"
            self.create_text(width / 2, height / 2, text=text, width=width - 30, fill="#6b7280")
            return
        self._time_max = max(self.result.duration_s, 1e-6)
        if self.mode == "raw":
            panel_height = max(145, height / len(self.selected_names))
            self.configure(scrollregion=(0, 0, width, panel_height * len(self.selected_names)))
            for i, name in enumerate(self.selected_names):
                field, unit, _scale = PLOTS[name]
                values = self._series(name)
                low, high = self._range(values, soc=field == "soc")
                axes = self._draw_axes(i * panel_height, panel_height, width, low, high, self._label(name), unit)
                self._draw_line(name, values, axes, low, high)
        else:
            self._draw_overlay(width, height)

    def _draw_overlay(self, width, height):
        """不同物理单位不能共用原值 y 轴；叠加图统一换算为相对初值。"""
        series = {name: relative_to_initial(self._series(name)) for name in self.selected_names}
        skipped = [name for name, values in series.items() if values is None]
        valid = {name: values for name, values in series.items() if values is not None}
        # 图例自然按可用宽度折行，颜色与原值分图相同。
        x, y = 10, 12
        for name in valid:
            label = self._label(name)
            item_width = max(104, len(label) * 14 + 30)
            if x + item_width > width - 15 and x > 10:
                x, y = 10, y + 22
            self.create_line(x, y, x + 16, y, fill=COLORS[name], width=3)
            self.create_text(x + 20, y, text=label, anchor="w", fill=COLORS[name])
            x += item_width
        if skipped:
            self.create_text(10, y + 24, anchor="nw", width=width - 30, fill="#92400e",
                             text="初值为0或相对值不可用，未叠加：" + "、".join(skipped))
            y += 50 + 18 * (len("、".join(skipped)) // max(1, int((width - 30) / 14)))
        if not valid:
            return
        y0 = y + 20
        panel_height = max(170, height - y0)
        self.configure(scrollregion=(0, 0, width, y0 + panel_height))
        low = min(min(v) for v in valid.values())
        high = max(max(v) for v in valid.values())
        low, high = self._range([low, high])
        axes = self._draw_axes(y0, panel_height, width, low, high, "相对初值（初值=100%）", "%")
        for name, values in valid.items():
            self._draw_line(name, values, axes, low, high)

    def _inspect(self, event):
        """标记最近的真实样本，读取原始单位；不把显示抽样或百分比当作实测值。"""
        if not self._axes or not self.result:
            return
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        active = next((a for a in self._axes if a[0] <= x <= a[1] and a[2] <= y <= a[3]), None)
        self.delete("cursor")
        if active is None:
            return
        target = (x - active[0]) / (active[1] - active[0]) * self._time_max
        index = min(len(self._times) - 1, bisect.bisect_left(self._times, target))
        if index and abs(self._times[index - 1] - target) < abs(self._times[index] - target):
            index -= 1
        for left, right, top, bottom in self._axes:
            cursor_x = left + self._times[index] / self._time_max * (right - left)
            self.create_line(cursor_x, top, cursor_x, bottom, fill="#9ca3af", dash=(3, 3), tags="cursor")
        values = [f"t = {self._times[index]:.3f} s（{self._times[index] / 60:.3f} min）"]
        point = self.result.points[index]
        if point.model_extrapolated:
            values.append("外推样本（未验证）")
        for name in self.selected_names:
            field, unit, scale = PLOTS[name]
            values.append(f"{self._label(name)} {getattr(point, field) * scale:.4g} {unit}")
        if self.on_inspect:
            self.on_inspect("； ".join(values))


class TimeSeriesPanel(ttk.Frame):
    """图表的勾选、显示模式、滚动和原值读数都封装在本模块。"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.mode_var = tk.StringVar(master=self, value="raw")
        self.selection_vars = {name: tk.BooleanVar(master=self, value=name in DEFAULT_PLOTS) for name in PLOTS}
        options = ttk.LabelFrame(self, text="显示参数（可多选；动力电流/功率为全部旋翼合计）", padding=5)
        options.pack(fill="x")
        for i, (name, variable) in enumerate(self.selection_vars.items()):
            ttk.Checkbutton(options, text=name, variable=variable, command=self.refresh_selection).grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 12), pady=1)
        mode = ttk.Frame(self)
        mode.pack(fill="x", pady=6)
        ttk.Radiobutton(mode, text="原值分图", variable=self.mode_var, value="raw", command=self.refresh_selection).pack(side="left")
        ttk.Radiobutton(mode, text="归一化叠加", variable=self.mode_var, value="relative", command=self.refresh_selection).pack(side="left", padx=8)
        ttk.Button(mode, text="常用四项", command=lambda: self.select(DEFAULT_PLOTS)).pack(side="right")
        # 固定高度的只读文本避免鼠标读数换行时使图表上下跳动；内容可选择/复制。
        self.inspection = tk.Text(self, height=3, wrap="word", relief="flat", background="#f3f4f6",
                                  font=("Microsoft YaHei UI", 9), state="disabled")
        self.inspection.pack(side="bottom", fill="x", pady=(6, 0))
        area = ttk.Frame(self)
        area.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(area, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.plot = PlotCanvas(area, width=420, height=330, yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self.plot.yview)
        self.plot.pack(fill="both", expand=True)
        self.plot.on_inspect = self._show_inspection
        self._show_inspection("移动鼠标读取原始数值；分图可上下滚动。叠加图为各自初值的百分比，不混用物理单位。")

    def select(self, names):
        """只改变图表，不清空仿真结果。供快捷按钮和自动检查调用。"""
        for name, variable in self.selection_vars.items():
            variable.set(name in names)
        self.refresh_selection()

    def refresh_selection(self):
        names = [name for name, variable in self.selection_vars.items() if variable.get()]
        self.plot.configure_series(names, self.mode_var.get())
        self._show_inspection("原值分图：每图有独立单位，共享时间范围，可滚动查看。" if self.mode_var.get() == "raw" else
                              "叠加图：y(t)/y(0)×100%；初值为0的曲线不叠加。移动鼠标仍读取原始单位。")

    def _show_inspection(self, text):
        self.inspection.configure(state="normal")
        self.inspection.delete("1.0", "end")
        self.inspection.insert("1.0", text)
        self.inspection.configure(state="disabled")

    def destroy(self):
        """勾选变量也在主线程释放，延续主窗口的 Tk 生命周期约束。"""
        self.plot.on_inspect = None
        super().destroy()
        self.selection_vars.clear()
        self.mode_var = None
