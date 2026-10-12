"""Catalog → exact cell → exact test; read-only, no adoption changes."""
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox
from battery_discharge.selection import BatterySelection
from platform_config import resolve_source_path
from .cell_database import load_cell_test, read_cell_selection, current_window


class CellDatabaseDialog(tk.Toplevel):
    def __init__(self, master, path, on_selected):
        with BatterySelection(path) as api:
            categories, samples = api.list_categories(), api.list_samples()
        super().__init__(master)
        self.path, self.on_selected = path, on_selected
        self.categories, self.samples = categories, samples
        self.sample_id = self.preview_digest = None
        self.title('选择电芯及放电实验')
        self.geometry('1060x650')
        self.minsize(920, 600)
        self.transient(master)
        self.partial = tk.BooleanVar(master=self, value=False)
        self.ripple = tk.BooleanVar(master=self, value=False)
        self.info = tk.StringVar(master=self)
        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        self.selectors = {}
        for label, key, callback in [('类型', 'type', self.select_type), ('类别 / 型号', 'category', self.select_category), ('具体电芯', 'sample', self.select_sample)]:
            row = ttk.Frame(body)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=label, width=13).pack(side='left')
            box = ttk.Combobox(row, state='readonly')
            box.pack(side='left', fill='x', expand=True)
            box.bind('<<ComboboxSelected>>', callback)
            self.selectors[key] = box
        row = ttk.Frame(body)
        row.pack(fill='x', pady=8)
        ttk.Label(row, text='本次选择仅用于当前模型，与数据库采用标记分别保存。').pack(side='left')
        ttk.Button(row, text='重新读取', command=self.refresh).pack(side='right')
        frame = ttk.Frame(body)
        frame.pack(fill='both', expand=True)
        columns = ('id', 'date', 'current', 'capacity', 'cutoff', 'state')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse', height=8)
        for key, label, width in zip(columns, ('实验编号', '测试日期', '电流 A', '容量 Ah', '截止 V', '曲线状态'), (80, 170, 90, 100, 90, 200)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=65)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.details)
        info_label = ttk.Label(body, textvariable=self.info, wraplength=860, justify='left')
        info_label.pack(fill='x', pady=10)
        info_label.bind('<Configure>', lambda event: info_label.configure(wraplength=max(200, event.width-8)))
        ttk.Checkbutton(body, text='仅用已记录窗口（允许截止未知或末端偏差超过 0.05 V，不补齐曲线）', variable=self.partial).pack(anchor='w', pady=6)
        ttk.Checkbutton(body, text='允许短时电流波动近似（累计≤1%时长、连续≤10秒、最大偏差≤50%，保留原测点）', variable=self.ripple).pack(anchor='w', pady=6)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x')
        self.apply_button = ttk.Button(buttons, text='使用这次实验构建模型', command=self.apply, state='disabled')
        self.apply_button.pack(side='right')
        ttk.Button(buttons, text='取消', command=self.destroy).pack(side='right', padx=8)
        self.selectors['type'].configure(values=('18650', '21700', '其他'))
        self.selectors['type'].set('18650')
        self.select_type()
        self.grab_set()

    def clear_test(self):
        self.tree.delete(*self.tree.get_children())
        self.preview_digest = None
        self.partial.set(False)
        self.ripple.set(False)
        self.apply_button.configure(state='disabled')

    def select_type(self, _event=None):
        kind = self.selectors['type'].get()
        self.category_rows = [r for r in self.categories if (r['form_factor'] not in ('18650', '21700') if kind == '其他' else r['form_factor'] == kind)]
        box = self.selectors['category']
        box.configure(values=tuple(f"{r['form_factor']} · {r['display_name']}" for r in self.category_rows))
        box.set('')
        if self.category_rows:
            box.current(0)
        self.select_category()

    def select_category(self, _event=None):
        index = self.selectors['category'].current()
        cid = self.category_rows[index]['category_id'] if index >= 0 else None
        self.sample_rows = [r for r in self.samples if r['category_id'] == cid]
        box = self.selectors['sample']
        box.configure(values=tuple(f"{r['display_name']} · {r['curve_count']} 次实验 · " + ('有可用曲线' if r['simulation_available'] else '暂无可用曲线') for r in self.sample_rows))
        box.set('')
        if self.sample_rows:
            box.current(0)
        self.select_sample()

    def select_sample(self, _event=None):
        self.clear_test()
        index = self.selectors['sample'].current()
        if index < 0:
            self.sample_id = None
            self.info.set('当前类别没有电芯资料。')
            return
        sample = self.sample_rows[index]
        self.sample_id = sample['id']
        try:
            with BatterySelection(self.path) as api:
                curves = api.list_curves(self.sample_id)
            for curve in curves:
                state = '可选择' if curve['available'] else '不可用：' + (curve['unavailable_reason'] or '待审核')
                current = curve.get('set_current_a')
                if current is None:
                    current = f"实测 {curve.get('measured_current_a') or '未知'}"
                values = (curve['test_id'], curve.get('test_date'), current, curve.get('total_capacity_ah'), curve.get('cutoff_voltage_v'), state)
                self.tree.insert('', 'end', iid=str(curve['test_id']), values=tuple('未知' if v is None else v for v in values))
            self.info.set(f"{sample['display_name']}\n重量：{sample.get('weight_g') or '未记录'} g；交流内阻：{sample.get('ac_resistance_mohm') or '未记录'} mΩ（仅参考）。\n" + ('请选择一次实验查看末端电压与测试条件。' if curves else '已保留电芯资料，尚无放电曲线，暂不能用于仿真。'))
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.info.set(str(exc))

    def details(self, _event=None):
        selected = self.tree.selection()
        self.preview_digest = None
        self.partial.set(False)
        self.ripple.set(False)
        self.apply_button.configure(state='disabled')
        if not selected:
            return
        try:
            cell, test, points, digest = read_cell_selection(self.path, self.sample_id, int(selected[0]))
            start, end, reference, tolerance, ripple = current_window(points)
            cutoff, raw_end = test.get('cutoff_voltage_v'), points[-1]['voltage_v']
            active = [p for p in points if p['current_a'] > 0]
            loaded_end = active[-1]['voltage_v'] if active else None
            delta = f'{loaded_end-cutoff:+.3f} V' if loaded_end is not None and cutoff is not None else '无法比较'
            basis = test['metadata'].get('cutoff_voltage_source') or '未记录'
            self.info.set(f"最后带载电压：{loaded_end} V；最后采样：{raw_end:g} V；带载末端−截止：{delta}\n截止标注来源：{basis}；交流内阻：{cell.get('ac_resistance_mohm', '未记录')} mΩ（仅参考）。\n源文件：{resolve_source_path(test['source_file'], 'battery')}\n建模时按实测电流积分，并核对恒流段、采样缺口与截止范围。")
            self.preview_digest = digest
            self.info.set(self.info.get() + f"\n参考电流 {reference:g} A；波动超限 {ripple['outside_tolerance_count']} 点，占 {100*ripple['outside_time_fraction']:.3f}% 时长。")
            self.apply_button.configure(state='normal')
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.info.set(str(exc))

    def refresh(self):
        try:
            with BatterySelection(self.path) as api:
                categories, samples = api.list_categories(), api.list_samples()
            self.categories, self.samples = categories, samples
            self.select_type()
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('读取失败', str(exc), parent=self)

    def apply(self):
        try:
            selected = self.tree.selection()
            if not selected or self.preview_digest is None:
                raise ValueError('请选择具体电芯的一次有效放电实验。')
            record = load_cell_test(self.path, self.sample_id, int(selected[0]), allow_partial=self.partial.get(), allow_ripple=self.ripple.get())
            if record['source']['selection_sha256'] != self.preview_digest:
                self.details()
                raise ValueError('实验在预览后发生变化，已重新载入详情，请核对后再次选择。')
            self.on_selected(record)
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('无法选择', str(exc), parent=self)
            return
        self.destroy()

    def destroy(self):
        parent = self.master
        super().destroy()
        self.partial = self.ripple = self.info = None
        if parent.winfo_exists():
            parent.grab_set()
