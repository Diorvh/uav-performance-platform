"""电池组模型库窗口：配置保存与用于仿真分开，缺曲线也能保存草稿。"""

from dataclasses import asdict
from pathlib import Path
import math
import sqlite3
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .battery_library import BatteryLibrary, cell_record, curve_from_record, record_id
from .battery_pack import BatteryPack
from .io_utils import load_curve_csv
from .cell_database import DEFAULT_CELL_DATABASE, verify_cell_source
from .cell_database_dialog import CellDatabaseDialog


class BatteryDialog(tk.Toplevel):
    def __init__(self, master, on_selected, library=None):
        super().__init__(master)
        self.title("电池组模型库")
        self.geometry("980x710")
        self.minsize(900, 650)
        self.transient(master)
        self.library = library or BatteryLibrary()
        self.on_selected = on_selected
        self.key = self.revision = None
        self.source = None
        self.vars = {}
        try:
            self.packs, self.sources = self.library.load()
        except (ValueError, OSError):
            super().destroy()
            raise
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=245)
        left.pack(side="left", fill="y", padx=(0, 16))
        ttk.Label(left, text="已保存的电池组").pack(anchor="w")
        self.tree = ttk.Treeview(left, columns=("name", "config"), show="headings", selectmode="browse", height=20)
        self.tree.heading("name", text="名称")
        self.tree.heading("config", text="配置")
        self.tree.column("name", width=155)
        self.tree.column("config", width=75)
        self.tree.pack(fill="both", expand=True, pady=8)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        ttk.Button(left, text="新建电池组", command=self.new).pack(fill="x")
        ttk.Label(left, text="选中后编辑，点击保存才会更新。\n更改名称后“另存为新模型”可保留原配置。",
                  wraplength=240, foreground="#6b7280").pack(fill="x", pady=8)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        pages = ttk.Notebook(right)
        pages.pack(fill="both", expand=True)
        config = ttk.Frame(pages, padding=10)
        data = ttk.Frame(pages, padding=10)
        pages.add(config, text="电池组配置")
        pages.add(data, text="单电芯放电数据")
        fields = (
            ("name", "电池组名称", ""), ("cell_name", "电芯型号", ""),
            ("series", "串联数 S", "3"), ("parallel", "并联数 P", "1"),
            ("cell_mass_g", "单体重量 / g（实测整包时可留空）", ""),
            ("extra_mass_g", "连接件等附加重量 / g", "0"),
            ("measured_mass_g", "整包实测重量 / g（优先，留空则估算）", ""),
            ("cell_resistance_mohm", "单体增量电阻 / mΩ（0=不修正）", ""),
            ("wiring_resistance_mohm", "整包线束电阻 / mΩ", "0"),
            ("cell_cutoff_v", "单体等效截止 / V（整包阈值÷S）", ""),
            ("notes", "备注", ""),
        )
        self.defaults = {name: default for name, _, default in fields}
        form = ttk.Frame(config)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (name, label, default) in enumerate(fields):
            variable = self.vars[name] = tk.StringVar(master=self, value=default)
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=variable, width=20).grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(config, text="整包重量 = S×P×单体重量 + 附加重量；填写整包实测重量后直接使用该值。\n"
                  "单体交流内阻不能自动视为直流增量电阻。缺少曲线、电阻或截止电压仍可保存配置，补齐后才能仿真。",
                  wraplength=570, justify="left", foreground="#6b7280").pack(fill="x", pady=12)
        self.reference_var = tk.StringVar(master=self, value="10")
        line = ttk.Frame(data)
        line.pack(fill="x")
        ttk.Label(line, text="单电芯实测恒流 / A：").pack(side="left")
        ttk.Entry(line, textvariable=self.reference_var, width=10).pack(side="left")
        ttk.Button(line, text="导入单电芯 CSV", command=self.import_csv).pack(side="left", padx=10)
        ttk.Button(data, text="从电芯数据库选择电池 / 实验", command=self.import_database).pack(anchor="w", pady=8)
        ttk.Label(data, text="CSV 必须是单个电芯的恒流放电，列为 time_s, voltage_V。\n"
                  "上方电流只用于下一次 CSV 导入。数据库入口使用实测电流积分，不使用这个手填值。",
                  wraplength=570, justify="left").pack(fill="x", pady=12)
        self.source_var = tk.StringVar(master=self)
        self.source_box = ttk.Combobox(data, textvariable=self.source_var, state="readonly")
        self.source_box.pack(fill="x")
        self.source_box.bind("<<ComboboxSelected>>", self.choose_source)
        ttk.Button(data, text="清除当前曲线关联（保留库中数据）", command=self.clear_source).pack(anchor="w", pady=8)
        self.source_info = tk.StringVar(master=self, value="待补充电芯数据")
        ttk.Label(data, textvariable=self.source_info, wraplength=570, justify="left").pack(fill="x", pady=8)
        self.status = tk.StringVar(master=self, value="新建配置可先保存，之后再补选单电芯数据。")
        ttk.Label(right, textvariable=self.status, wraplength=590, foreground="#92400e", justify="left").pack(fill="x", pady=8)
        actions = ttk.Frame(right)
        actions.pack(fill="x")
        for label, command in (("保存配置", self.save), ("另存为新模型", self.save_as),
                               ("保存并用于仿真", self.apply), ("关闭", self.destroy)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 6))
        self.populate()
        self.grab_set()

    def populate(self):
        self.tree.delete(*self.tree.get_children())
        for key, (pack, _) in self.packs.items():
            self.tree.insert("", "end", iid=key, values=(pack.name, f"{pack.series}S{pack.parallel}P"))
        self.source_choices = {f"{r['name']} · {r['reference_current_a']:g} A · {key[:8]}": key
                               for key, r in self.sources.items()}
        self.source_box.configure(values=tuple(self.source_choices))

    def new(self):
        self.key = self.revision = None
        self.tree.selection_remove(*self.tree.selection())
        for key, value in self.defaults.items():
            self.vars[key].set(value)
        self.clear_source()
        self.status.set("新建电池组。未保存的编辑不会写入模型库。")

    def select(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        self.key = selected[0]
        profile, self.revision = self.packs[self.key]
        for key, value in asdict(profile).items():
            if key in self.vars:
                self.vars[key].set("" if value is None else str(value))
        self.source = self.sources.get(profile.cell_data_id)
        self.show_source()
        self.status.set(f"已载入 {profile.name}；整包重量 {profile.mass_g:.2f} g。编辑后请保存。")

    def show_source(self):
        self.source_var.set("")
        if self.source is None:
            self.source_info.set("待补充电芯数据：当前只保存配置，不能仿真。")
            return
        key = record_id(self.source)
        for label, candidate in self.source_choices.items():
            if candidate == key:
                self.source_var.set(label)
        curve = curve_from_record(self.source)
        source = self.source['source']
        if source.get('type') == 'single-cell SQLite':
            origin = (f"数据库实验 {source['test_id']}；末端 {source['stable_end_voltage_v']:g} V；"
                      f"截止设定 {source['cutoff_voltage_v'] if source['cutoff_voltage_v'] is not None else '未知'} V（{source.get('cutoff_voltage_source', '来源未记录')}）\n"
                      f"起停阶段未纳入 {source['trimmed_start_count']} / {source['trimmed_end_count']} 点；"
                      + ('仅使用记录窗口，未验证到达截止。' if source['partial_window'] else '在记录支持范围内使用。')
                      + f"\n交流内阻 {source['cell_metadata'].get('ac_resistance_mohm', '未知')} mΩ，仅参考。")
            if source.get('approximate_current'):
                origin += '\n已允许短时电流波动近似，原始测点保留。'
        else:
            origin = str(source)
        self.source_info.set(f"电芯：{self.source['name']}\n参考恒流 {curve.reference_current_a:g} A，"
                             f"测试窗口 {curve.usable_capacity_ah:.3f} Ah，{len(curve.points)} 点\n"
                             f"来源：{origin}\n保存的是单电芯曲线快照；数据库记录变化后需重新选择。")

    def clear_source(self):
        self.source = None
        self.show_source()

    def choose_source(self, _event=None):
        self.accept_cell_record(self.sources[self.source_choices[self.source_var.get()]])

    def accept_cell_record(self, record):
        """Retain pack parameters for the same cell; clear mismatched parameters."""
        curve_from_record(record)
        source = record['source']
        previous = self.source['source'] if self.source else {}
        same_cell = (source.get('type') == 'single-cell SQLite'
                     and previous.get('type') == source['type']
                     and previous.get('database_path') == source.get('database_path')
                     and previous.get('sample_id') == source.get('sample_id'))
        self.source = record
        self.vars['cell_name'].set(record['name'])
        if source.get('type') == 'single-cell SQLite':
            self.vars['cell_cutoff_v'].set('' if source['cutoff_voltage_v'] is None else str(source['cutoff_voltage_v']))
            if not same_cell:
                mass = source['cell_metadata'].get('weight_g')
                valid_mass = isinstance(mass, (float, int)) and not isinstance(mass, bool) and math.isfinite(mass) and mass > 0
                self.vars['cell_mass_g'].set(str(mass) if valid_mass else '')
                self.vars['cell_resistance_mohm'].set('')
                self.vars['measured_mass_g'].set('')
            self.status.set('已关联所选电芯实验；请核对重量，并明确填写单体增量电阻（0=不修正）。')
        self.show_source()

    def import_database(self):
        try:
            CellDatabaseDialog(self, DEFAULT_CELL_DATABASE, self.accept_cell_record)
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('读取失败', str(exc), parent=self)

    def import_csv(self):
        try:
            name = self.vars["cell_name"].get().strip()
            if not name:
                raise ValueError("请先在配置页填写电芯型号。")
            reference = float(self.reference_var.get())
            path = filedialog.askopenfilename(parent=self, title="导入单电芯恒流放电 CSV", filetypes=(("CSV", "*.csv"),))
            if not path:
                return
            curve = load_curve_csv(path, reference)
            self.accept_cell_record(cell_record(curve, name, {"type": "single-cell CSV", "path": str(Path(path).resolve())}))
        except (ValueError, OSError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)

    def profile(self):
        data = {}
        for key, variable in self.vars.items():
            raw = variable.get().strip()
            data[key] = raw if key in ("name", "cell_name", "notes") else (
                int(raw) if key in ("series", "parallel") else float(raw) if raw else None)
        if data["extra_mass_g"] is None or data["wiring_resistance_mohm"] is None:
            raise ValueError("附加重量和线束电阻不能为空；没有时明确填写 0。")
        if self.source is not None and data["cell_name"] != self.source["name"]:
            raise ValueError("电芯型号与已选曲线不一致；请重新选择匹配曲线，或清除关联后保存配置。")
        data["cell_data_id"] = record_id(self.source) if self.source is not None else None
        return BatteryPack(**data)

    def persist(self, profile, new=False):
        if self.source is not None:
            verify_cell_source(self.source)
        key, revision = self.library.save(profile, key=None if new else self.key,
                                          revision=None if new else self.revision, source=self.source)
        self.key, self.revision = key, revision
        self.packs, self.sources = self.library.load()
        self.populate()
        self.show_source()
        self.status.set(f"已保存：{profile.name} · {profile.series}S{profile.parallel}P · {profile.mass_g:.2f} g"
                        + (" · 待补充电芯数据" if self.source is None else ""))

    def save(self, new=False):
        try:
            self.persist(self.profile(), new)
        except (ValueError, OSError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)

    def save_as(self):
        self.save(new=True)

    def apply(self):
        try:
            profile = self.profile()
            if self.source is None:
                raise ValueError("尚未关联单电芯放电曲线；可先保存配置，补齐数据后再用于仿真。")
            curve = profile.build_curve(curve_from_record(self.source))
            self.persist(profile)
            self.on_selected(profile, curve, {"library_path": str(self.library.path), "profile_id": self.key,
                                             "revision": self.revision, "profile": profile.metadata(),
                                             "cell_source": self.source})
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法使用", str(exc), parent=self)
            return
        self.destroy()

    def destroy(self):
        super().destroy()
        self.vars.clear()
        for key, value in list(vars(self).items()):
            if isinstance(value, tk.Variable):
                setattr(self, key, None)
