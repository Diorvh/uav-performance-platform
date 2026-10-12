"""数据库曲线选择窗口；只显示/选择，不提供写库或修改采用标记的功能。"""

import math
import tkinter as tk
from tkinter import messagebox, ttk

from .database_adapter import HARDWARE_COLUMNS, build_propulsion, read_database


class DatabaseDialog(tk.Toplevel):
    """选择同一硬件配置下的多电压实验，明确显示缺点和采用冲突。"""

    def __init__(self, master, snapshot, on_selected):
        super().__init__(master)
        self.title("从力效数据库选择推进曲线 · 只读")
        self.geometry("1000x580")
        self.minsize(900, 500)
        self.transient(master)
        self.snapshot = snapshot
        self.on_selected = on_selected
        self.filter_var = tk.StringVar(master=self, value="全部配置（请手动选择实验组）")
        self.info_var = tk.StringVar(master=self)
        self.model_var = tk.StringVar(master=self, value="共享电压指数拟合")
        self.extrapolation_var = tk.BooleanVar(master=self, value=False)
        self.detail_var = tk.StringVar(master=self, value="选择一组查看来源；Ctrl/Shift 多选不同电压层，再点击载入。")
        self.families = {}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        line = ttk.Frame(body)
        line.pack(fill="x")
        self.filter_box = ttk.Combobox(line, textvariable=self.filter_var, state="readonly", width=88)
        self.filter_box.pack(side="left", fill="x", expand=True)
        self.filter_box.bind("<<ComboboxSelected>>", lambda _e: self._populate())
        ttk.Button(line, text="重新读取", command=self.refresh).pack(side="right", padx=(8, 0))
        info = ttk.Label(body, textvariable=self.info_var, wraplength=950, justify="left")
        info.pack(fill="x", pady=8)
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        columns = ("order", "motor", "prop", "voltage", "count", "status", "repeat", "i0")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended", height=12)
        for name, title, width in zip(columns, ("序号 / ID", "电机", "桨叶", "电压 V", "点数", "采用状态", "重复集", "I0 / A"),
                                       (95, 185, 125, 75, 55, 115, 65, 65)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=40, stretch=name in ("motor", "prop"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._details)
        self.tree.tag_configure("blocked", foreground="#92400e")
        detail = ttk.Label(body, textvariable=self.detail_var, wraplength=950, justify="left")
        detail.pack(fill="x", pady=8)
        detail.bind("<Configure>", lambda e: detail.configure(wraplength=max(100, e.width - 10)))
        options = ttk.Frame(body)
        options.pack(fill="x", pady=(0, 6))
        ttk.Label(options, text="推进模型：").pack(side="left")
        model_box = ttk.Combobox(options, textvariable=self.model_var, state="readonly", width=22,
                                values=("共享电压指数拟合", "实测分层插值（对照）"))
        model_box.pack(side="left")
        self.extrapolation_button = ttk.Checkbutton(options, text="允许 7.5～12.6 V 电压外推（未验证）",
                                                    variable=self.extrapolation_var)
        self.extrapolation_button.pack(side="left", padx=12)
        model_box.bind("<<ComboboxSelected>>", self._model_changed)
        ttk.Label(body, text="净功率口径保持不变。拟合需至少两层、每层≥3点及约50gf共同区间；转速为T=k×rpm²估算，不代表最大可达转速。",
                  foreground="#92400e", wraplength=950).pack(fill="x", pady=(0, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="载入选中曲线", command=self.apply_selection).pack(side="right")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=8)
        self._set_snapshot(snapshot)
        self.grab_set()

    def _model_changed(self, _event=None):
        fitted = self.model_var.get() == "共享电压指数拟合"
        if not fitted:
            self.extrapolation_var.set(False)
        self.extrapolation_button.configure(state="normal" if fitted else "disabled")

    def _set_snapshot(self, snapshot):
        """刷新后清空选中项，不沿用可能失效的 group_id。"""
        self.snapshot = snapshot
        self.families = {"全部配置（请手动选择实验组）": None}
        for group in snapshot.groups:
            key = tuple(group[k] for k in HARDWARE_COLUMNS)
            label = (f"{key[0]} | {key[1]} | 进角={key[2]}° / PWM={key[3]}kHz / AM32={key[4]}")
            self.families[label] = key
        self.filter_box.configure(values=tuple(self.families))
        if self.filter_var.get() not in self.families:
            self.filter_var.set(next(iter(self.families)))
        pending = [f"R{r:03d}" for r, count in snapshot.repeat_adopted_counts.items() if count == 0]
        conflict = [f"R{r:03d}" for r, count in snapshot.repeat_adopted_counts.items() if count > 1]
        self.info_var.set(f"{len(snapshot.groups)} 组 / {len(snapshot.measurements)} 点；可采用 {len(snapshot.eligible_groups)} 组。\n"
                          f"待人工采用：{', '.join(pending) or '无'}；多重采用冲突：{', '.join(conflict) or '无'}。")
        self._populate()

    def _populate(self):
        """过滤不改变采用状态；未采用和单点组仍展示以便追溯。"""
        for item in self.tree.get_children():
            self.tree.delete(item)
        family = self.families.get(self.filter_var.get())
        for group in self.snapshot.groups:
            if family is not None and tuple(group[k] for k in HARDWARE_COLUMNS) != family:
                continue
            count = len(self.snapshot.points_for(group["id"]))
            state = self.snapshot.status(group)
            if count < 2:
                state += " / 单点"
            repeat = group["repeat_set"]
            self.tree.insert("", "end", iid=str(group["id"]), values=(
                f"{group['display_order']:03d} / {group['id']}", group["motor_spec"], group["propeller"],
                group["nominal_voltage_v"], count, state, "—" if repeat is None else f"R{repeat:03d}",
                group["no_load_current_a"]), tags=() if state == "可采用" else ("blocked",))

    def _details(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        group = next(g for g in self.snapshot.groups if g["id"] == int(selected[0]))
        forces = [p["thrust_mn"] / 9.80665 for p in self.snapshot.points_for(group["id"])
                  if isinstance(p["thrust_mn"], (int, float)) and math.isfinite(p["thrust_mn"])]
        support = f"{min(forces):.1f}～{max(forces):.1f} gf" if forces else "无有效值"
        self.detail_var.set(f"已选 {len(selected)} 组。首组来源：{group['source_file']} / {group['source_sheet']} / {group['source_anchor']}\n"
                            f"进角={group['esc_advance_deg']}°；PWM={group['pwm_frequency_khz']} kHz；AM32 kV={group['am32_motor_kv']}；推力范围：{support}。")

    def refresh(self):
        try:
            self._set_snapshot(read_database(self.snapshot.path))
        except (OSError, ValueError) as exc:
            messagebox.showerror("读取失败", str(exc), parent=self)

    def apply_selection(self):
        """确认时重新核对数据库，避免在旧采用状态上建模。"""
        try:
            fresh = read_database(self.snapshot.path)
            if fresh.sha256 != self.snapshot.sha256:
                self._set_snapshot(fresh)
                raise ValueError("数据库已变化，列表已刷新。请重新确认实验组。")
            selected = build_propulsion(fresh, [int(i) for i in self.tree.selection()],
                                        model_kind="shared_fit" if self.model_var.get() == "共享电压指数拟合" else "interpolation",
                                        allow_extrapolation=self.extrapolation_var.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法载入", str(exc), parent=self)
            return
        self.on_selected(selected)
        self.destroy()

    def destroy(self):
        """在主线程释放 Tk 变量，避免窗口引用环留到计算线程的 GC 阶段。"""
        super().destroy()
        self.filter_var = self.info_var = self.detail_var = None
        self.model_var = self.extrapolation_var = None
