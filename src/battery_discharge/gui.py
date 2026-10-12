from __future__ import annotations
import json
import math
import os
import queue
import re
import threading
from pathlib import Path
from platform_config import state_path, battery_mapping_paths, resolve_source_path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from matplotlib import rcParams
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from .models import POINT_FIELDS
from .excel_import import preview_workbooks

META_LABELS = {
 'form_factor':'电池类型','category_code':'类别','sample_code':'样品编号','model_name':'型号',
 'supplier':'来源','condition':'状态','nominal_capacity_ah':'标称容量 (Ah)',
 'nominal_voltage_v':'标称电压 (V)','weight_g':'重量 (g)','model_spec':'型号规格',
 'dimensions':'尺寸','price':'价格',
 'appearance':'外观','purchase_date':'购买日期','manufacturer':'厂家','rated_c_rate':'标称倍率 (C)',
 'ac_resistance_mohm':'交流内阻 (mΩ)','ac_resistance_voltage_v':'内阻测量电压 (V)',
 'ac_resistance_note':'内阻备注','usage':'用途','summary_code':'汇总表编号',
 'protocol_class':'测试类别','test_c_rate':'测试倍率 (C)',
 'test_date':'实验时间（首条原始记录）','filename_time':'文件名时间（来源）',
 'set_current_a':'设定电流 (A)','cutoff_voltage_v':'截止电压 (V)',
 'measured_current_a':'实测中位电流 (A)','last_voltage_v':'末端实测电压 (V)',
 'total_capacity_ah':'本次总容量 (Ah)','total_energy_wh':'本次总能量 (Wh)',
 'duration_s':'记录时长 (s)','initial_soc_pct':'初始 SOC (%)',
 'reference_capacity_ah':'SOC 参考容量 (Ah)','soc_basis':'SOC 口径',
 'mapping_source':'汇总表','mapping_sheet':'汇总表工作表','mapping_row':'汇总表源行','source_file':'原始文件','source_sheet':'工作表',
 'formula_version':'计算版本','max_gap_s':'采样间隔审查阈值 (s)',
 'mean_voltage_v':'放电加权平均电压 (V)'}


def shown(value):
    if value is None or value == '':
        return '—'
    if isinstance(value, float):
        return f'{value:.7g}'
    return str(value)


def category_name(row):
    return f"{shown(row.get('category_code'))}-{row.get('model_name') or '型号未确认'}"


def form_factor_label(value):
    value = str(value or '')
    if value in ('18650', '21700'):
        return value
    return '其他'


def sample_label(value):
    value = shown(value)
    return value[3:] if value.startswith('原编号') else value


def display_sample_code(row):
    """Use the complete catalog identifier, retaining legacy zero-padding and prefixes."""
    if row.get('summary_code') not in (None, ''):
        return sample_label(row.get('summary_code'))
    raw_sample = row.get('sample_code')
    sample = sample_label(raw_sample)
    raw_text = str(raw_sample or '')
    if raw_text.startswith('原编号'):
        return sample
    if form_factor_label(row.get('form_factor')) in ('18650', '21700') and row.get('category_code') not in (None, ''):
        return f'{row.get("category_code")}-{sample}'
    return sample


def test_name(row, sequence):
    sample = display_sample_code(row)
    capacity = row.get('total_capacity_ah')
    try:
        capacity_text = f'{float(capacity) * 1000:.0f}mAh'
    except (TypeError, ValueError):
        capacity_text = '容量未知'
    cutoff = row.get('cutoff_voltage_v')
    if cutoff in (None, ''):
        cutoff = row.get('last_voltage_v')
        cutoff_label = '末端'
    else:
        cutoff_label = '截止'
    try:
        cutoff_text = f'{cutoff_label}{float(cutoff):.2f}V'
    except (TypeError, ValueError):
        cutoff_text = f'{cutoff_label}电压未知'
    return f'{sample}-{sequence} · {capacity_text} · {cutoff_text}'


def safe_filename(value):
    """Return a Windows-safe file-name component while retaining readable IDs."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or '未命名'))
    return value.strip(' .') or '未命名'


class App:
    def __init__(self, root, database):
        self.root, self.database = root, database
        self.state_path = state_path('battery_discharge.gui-state.json')
        try:
            self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}
        self.preview = []
        self.rows = []
        self.catalog = []
        self.catalog_by_id = {}
        self.category_by_id = {}
        self.sample_tests = {}
        self.sample_tree_ids = {}
        self.selected_sample_id = None
        self.current_sample = None
        self.point_cache = {}
        self.results = queue.Queue()
        self.busy = False
        self.closed = False
        self.poll_id = None
        self.source = tk.StringVar(value='数据库')
        self.x = tk.StringVar(value=self.state.get('x','elapsed_s'))
        self.y = tk.StringVar(value=self.state.get('y','voltage_v'))
        for variable, fallback in ((self.x,'elapsed_s'),(self.y,'voltage_v')):
            if variable.get() not in POINT_FIELDS:
                variable.set(fallback)
        saved_filters = self.state.get('filters',{})
        state_version = self.state.get('version', 1)
        try:
            state_version = int(state_version)
        except (TypeError, ValueError):
            state_version = 1
        self.filters = {
            'form_factor': tk.StringVar(value=saved_filters.get('form_factor','')),
            'category_code': tk.StringVar(value=''),  # kept for old state compatibility
            'category_id': tk.StringVar(value=saved_filters.get('category_id','') if state_version >= 2 else ''),
        }
        self.legacy_category_code = saved_filters.get('category_code','') if state_version < 2 else ''
        self.form_factor_choices = {'18650':'18650','21700':'21700','其他':'__other__'}
        self.form_factor_display = tk.StringVar(value='全部')
        self.category_display = tk.StringVar(value='全部')
        self.category_choices = {}
        self.mapping_paths = battery_mapping_paths()
        root.title('电池放电特性数据库')
        try:
            root.geometry(self.state.get('geometry','1450x900'))
        except tk.TclError:
            root.geometry('1450x900')
        root.minsize(1080,720)
        root.protocol('WM_DELETE_WINDOW',self.close)
        ttk.Label(root,text='容量、能量和相对 SOC 以原始曲线积分为准；Excel 预览不会自动导入。',foreground='#8a5200',padding=8).pack(fill='x')
        bar = ttk.Frame(root,padding=(8,0,8,6));bar.pack(fill='x')
        self.open_button = ttk.Button(bar,text='选择实验文件 · 只读预览',command=self.open_preview)
        self.open_button.pack(side='left')
        ttk.Button(bar,text='选择型号汇总表',command=self.choose_mappings).pack(side='left',padx=6)
        ttk.Button(bar,text='刷新数据库',command=self.reload).pack(side='left',padx=4)
        ttk.Button(bar,text='全部电池一览',command=self.show_catalog).pack(side='left',padx=4)
        self.export_button = ttk.Button(bar,text='导出所选仿真曲线',command=self.export_simulation)
        self.export_button.pack(side='left',padx=4)
        ttk.Label(bar,text='显示').pack(side='left',padx=(12,4))
        choice=ttk.Combobox(bar,textvariable=self.source,values=['数据库','Excel 预览'],state='readonly',width=12)
        choice.pack(side='left');choice.bind('<<ComboboxSelected>>',lambda e:self.reload())
        self.status = tk.StringVar()
        status_bar = ttk.Frame(root, padding=(8, 0, 8, 4)); status_bar.pack(fill='x')
        ttk.Label(status_bar, textvariable=self.status).pack(side='left')
        self.panes=ttk.Panedwindow(root,orient='horizontal');self.panes.pack(fill='both',expand=True,padx=8,pady=8)
        left=ttk.Frame(self.panes,padding=6);right=ttk.Frame(self.panes,padding=6)
        self.panes.add(left,weight=1);self.panes.add(right,weight=4)
        group=ttk.LabelFrame(left,text='筛选条件',padding=5);group.pack(fill='x',pady=6)
        ttk.Label(group,text='电池类型').pack(anchor='w')
        self.form_factor_combo=ttk.Combobox(group,textvariable=self.form_factor_display,
            values=['18650','21700','其他'],state='readonly',width=36)
        self.form_factor_combo.pack(fill='x',pady=4)
        self.form_factor_combo.bind('<<ComboboxSelected>>',self.form_factor_changed)
        ttk.Label(group,text='电池类别 / 型号').pack(anchor='w',pady=(6,0))
        self.category_combo=ttk.Combobox(group,textvariable=self.category_display,state='readonly',width=36)
        self.category_combo.pack(fill='x',pady=4)
        self.category_combo.bind('<<ComboboxSelected>>',self.category_changed)
        ttk.Button(group,text='清除筛选',command=self.clear_filters).pack(anchor='e')
        ttk.Label(left,text='电池列表（编号 · 标称容量 · 曲线数）').pack(anchor='w',pady=(8,4))
        battery_box=ttk.Frame(left);battery_box.pack(fill='both',expand=True)
        battery_columns=('sample_code','capacity_mah','curve_count')
        self.battery_table=ttk.Treeview(battery_box,columns=battery_columns,show='headings',selectmode='browse',height=8)
        battery_heads={'sample_code':'编号','capacity_mah':'容量 (mAh)','curve_count':'曲线数'}
        battery_widths={'sample_code':125,'capacity_mah':105,'curve_count':70}
        for key in battery_columns:
            self.battery_table.heading(key,text=battery_heads[key])
            self.battery_table.column(key,width=battery_widths[key],stretch=key=='sample_code')
        bsy=ttk.Scrollbar(battery_box,command=self.battery_table.yview)
        self.battery_table.configure(yscrollcommand=bsy.set)
        bsy.pack(side='right',fill='y');self.battery_table.pack(fill='both',expand=True)
        self.battery_table.bind('<<TreeviewSelect>>',self.battery_changed)
        ttk.Label(left,text='实验（Ctrl / Shift 多选曲线；选中不等于采用）').pack(anchor='w',pady=(8,4))
        experiment_box=ttk.Frame(left);experiment_box.pack(fill='both',expand=True)
        self.listbox=tk.Listbox(experiment_box,selectmode='extended',exportselection=False,width=42,height=9)
        esy=ttk.Scrollbar(experiment_box,command=self.listbox.yview);self.listbox.configure(yscrollcommand=esy.set)
        esy.pack(side='right',fill='y');self.listbox.pack(fill='both',expand=True)
        self.listbox.bind('<<ListboxSelect>>',lambda e:self.selection_changed())
        mark=ttk.Frame(left);mark.pack(fill='x',pady=6)
        self.adopt=ttk.Button(mark,text='设为代表实验',command=lambda:self.mark(True));self.adopt.pack(side='left')
        self.unadopt=ttk.Button(mark,text='取消采用',command=lambda:self.mark(False));self.unadopt.pack(side='left',padx=5)
        self.notebook=ttk.Notebook(right);self.notebook.pack(fill='both',expand=True)
        catalog=ttk.Frame(self.notebook);plot=ttk.Frame(self.notebook);detail=ttk.Frame(self.notebook);points=ttk.Frame(self.notebook)
        self.notebook.add(catalog,text='电池资料');self.notebook.add(plot,text='曲线比较');self.notebook.add(detail,text='实验信息与异常');self.notebook.add(points,text='原始点与计算值')
        self._build_catalog_page(catalog)
        axes=ttk.Frame(plot);axes.pack(fill='x',pady=5)
        self.axis_vars=[]
        for title,var in [('横轴',self.x),('纵轴',self.y)]:
            ttk.Label(axes,text=title).pack(side='left',padx=4)
            labelvar=tk.StringVar(value=POINT_FIELDS[var.get()]);self.axis_vars.append(labelvar)
            cb=ttk.Combobox(axes,textvariable=labelvar,values=list(POINT_FIELDS.values()),state='readonly',width=27)
            cb.pack(side='left',padx=4)
            cb.bind('<<ComboboxSelected>>',lambda e,v=var,l=labelvar:self.axis_changed(v,l))
        rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];rcParams['axes.unicode_minus']=False
        self.figure=Figure(figsize=(8,5),dpi=100);self.ax=self.figure.add_subplot(111)
        self.canvas=FigureCanvasTkAgg(self.figure,master=plot)
        NavigationToolbar2Tk(self.canvas,plot).pack(fill='x')
        self.canvas.get_tk_widget().pack(fill='both',expand=True)
        self.detail=tk.Text(detail,wrap='word',font=('Microsoft YaHei UI',10),padx=12,pady=10)
        ds=ttk.Scrollbar(detail,command=self.detail.yview);self.detail.configure(yscrollcommand=ds.set)
        ds.pack(side='right',fill='y');self.detail.pack(fill='both',expand=True)
        query=ttk.Frame(points);query.pack(fill='x',pady=5)
        ttk.Label(query,text='查询所选首条实验：时间 (s)').pack(side='left')
        self.query_time=tk.StringVar();ttk.Entry(query,textvariable=self.query_time,width=14).pack(side='left',padx=4)
        ttk.Button(query,text='定位最近采样点',command=self.query_point).pack(side='left')
        self.query_result=tk.StringVar();ttk.Label(query,textvariable=self.query_result).pack(side='left',padx=5)
        tableframe=ttk.Frame(points);tableframe.pack(fill='both',expand=True)
        cols=['source_row','raw_time',*POINT_FIELDS]
        self.table=ttk.Treeview(tableframe,columns=cols,show='headings')
        for k in cols:
            self.table.heading(k,text={'source_row':'源行','raw_time':'原始时间'}.get(k,POINT_FIELDS.get(k,k)))
            self.table.column(k,width=int(self.state.get('widths',{}).get(k,180 if k=='raw_time' else 145)),stretch=False)
        sy=ttk.Scrollbar(tableframe,orient='vertical',command=self.table.yview)
        sx=ttk.Scrollbar(tableframe,orient='horizontal',command=self.table.xview)
        self.table.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        sy.pack(side='right',fill='y');sx.pack(side='bottom',fill='x');self.table.pack(fill='both',expand=True)
        self.reload()
        self.root.after_idle(self.restore_selection)

    def axis_changed(self,var,label):
        var.set(next(k for k,v in POINT_FIELDS.items() if v==label.get()));self.selection_changed()

    def restore_selection(self):
        try:
            self.panes.sashpos(0,int(self.state.get('sash',380)))
        except (tk.TclError,ValueError,TypeError):
            pass
        saved_sample = self.state.get('selected_sample_id')
        saved_sample = str(saved_sample) if saved_sample is not None else None
        saved_tests = {str(value) for value in self.state.get('selected_ids',[]) if value is not None}
        target = saved_sample if saved_sample in self.sample_tree_ids else None
        if target is None and saved_tests:
            for sample_id, rows in self.sample_tests.items():
                if any(str(row.get('id')) in saved_tests for row in rows):
                    target = str(sample_id)
                    break
        if target is not None:
            self.battery_table.selection_set(target)
            self.battery_table.focus(target)
            self._load_sample(target, select_first=False)
            for index, row in enumerate(self.rows):
                if str(row.get('id')) in saved_tests:
                    self.listbox.selection_set(index)
        self.selection_changed()

    def clear_filters(self):
        for var in self.filters.values():var.set('')
        self.selected_sample_id = None
        self.reload()

    def form_factor_changed(self, _event=None):
        self.filters['form_factor'].set(self.form_factor_choices.get(self.form_factor_display.get(),''))
        self.filters['category_code'].set('')
        self.filters['category_id'].set('')
        self.reload()

    def category_changed(self, _event=None):
        self.filters['category_id'].set(str(self.category_choices.get(self.category_display.get(),'') or ''))
        self.filters['category_code'].set('')
        self.selected_sample_id = None
        self.reload()

    def reload(self):
        if self.source.get() == 'Excel 预览':
            self._load_preview_catalog()
        else:
            self.catalog = self.database.samples()
            rows = self.database.tests()
            for row in rows:
                row['_sample_id'] = row.get('sample_id')
                row['_status'] = '已采用' if row.get('preferred') else '待选择'
            self._load_catalog_tests(rows)
        all_rows = [row for sample_rows in self.sample_tests.values() for row in sample_rows]
        if self.source.get() == '数据库':
            point_counts = {str(item['test_id']): int(item['count']) for item in self.database.connection.execute(
                'SELECT test_id, count(*) AS count FROM discharge_points GROUP BY test_id')}
        else:
            point_counts = {str(id(row)): len(row.get('_candidate',{}).get('points') or [])
                            for row in all_rows}
        selected_type = self.filters['form_factor'].get()
        available_types = {form_factor_label(row.get('form_factor')) for row in self.catalog}
        if selected_type not in ('18650','21700','__other__'):
            selected_type = '18650' if '18650' in available_types else (
                '21700' if '21700' in available_types else '__other__')
            self.filters['form_factor'].set(selected_type)
        type_label = '其他' if selected_type == '__other__' else selected_type
        catalog = [row for row in self.catalog if form_factor_label(row.get('form_factor')) == type_label]
        groups = {}
        for row in catalog:
            category_id = row.get('category_id')
            if category_id is None:
                category_id = f"preview:{row.get('form_factor','')}:{row.get('category_code','')}"
                row['category_id'] = category_id
            key = str(category_id)
            group = groups.setdefault(key, {'id': category_id, 'code': str(row.get('category_code') or ''),
                                             'models': set(), 'form_factor': row.get('form_factor')})
            group['models'].add(row.get('model_name') or '型号未确认')
        ordered = sorted(groups.values(), key=lambda item: (
            0, int(item['code']) if item['code'].isdigit() else item['code']) if item['code'].isdigit()
            else (1, item['code'], str(item['id'])))
        self.category_by_id = {str(item['id']): item for item in ordered}
        labels = []
        self.category_choices = {}
        seen_labels = set()
        for item in ordered:
            label = f"{item['code'] or '类别未确认'}-{' / '.join(sorted(item['models']))}"
            if label in seen_labels:
                label = f"{label}（{item['form_factor']}）"
            seen_labels.add(label)
            labels.append(label)
            self.category_choices[label] = item['id']
        self.category_combo.configure(values=['全部',*self.category_choices])
        selected = self.filters['category_id'].get()
        if not selected and self.legacy_category_code:
            old_matches = [item for item in ordered if item['code'] == str(self.legacy_category_code)]
            if len(old_matches) == 1:
                selected = str(old_matches[0]['id'])
                self.filters['category_id'].set(selected)
            self.legacy_category_code = ''
        if selected not in self.category_by_id:
            selected = ''
            self.filters['category_id'].set('')
        self.category_display.set(next((label for label, value in self.category_choices.items()
                                        if str(value) == str(selected)), '全部'))
        if selected:
            catalog = [row for row in catalog if str(row.get('category_id')) == str(selected)]
        self.form_factor_display.set(
            next((label for label,code in self.form_factor_choices.items() if code==selected_type), '全部')
        )
        self.catalog_by_id = {str(row.get('id')): row for row in catalog}
        self.sample_tree_ids = {}
        self.battery_table.delete(*self.battery_table.get_children())
        for row in catalog:
            sample_id = row.get('id')
            iid = str(sample_id)
            self.sample_tree_ids[iid] = sample_id
            tests = self.sample_tests.get(sample_id, self.sample_tests.get(iid, []))
            curve_count = sum(point_counts.get(str(test.get('id')), 0) > 0
                              if self.source.get() == '数据库'
                              else bool(test.get('_candidate',{}).get('points')) for test in tests)
            capacity = row.get('nominal_capacity_ah')
            try:
                capacity_text = f'{float(capacity) * 1000:.0f}'
            except (TypeError, ValueError):
                capacity_text = '—'
            self.battery_table.insert('', 'end', iid=iid, values=(display_sample_code(row), capacity_text, curve_count))
        self.point_cache = {}
        target = str(self.selected_sample_id) if self.selected_sample_id is not None else None
        if target not in self.sample_tree_ids:
            target = next(iter(self.sample_tree_ids), None)
        if target is None:
            self.selected_sample_id = None
            self.current_sample = None
            self.rows = []
            self.listbox.delete(0, 'end')
            self._render_catalog(None)
        else:
            self.battery_table.selection_set(target)
            self.battery_table.focus(target)
            self._load_sample(target, select_first=True)
        state = 'normal' if self.source.get() == '数据库' else 'disabled'
        self.adopt.configure(state=state);self.unadopt.configure(state=state)
        visible_sample_ids = {str(row.get('id')) for row in catalog}
        visible_tests = [row for row in all_rows if str(row.get('_sample_id')) in visible_sample_ids]
        self.status.set(f"{self.source.get()}：{len(catalog)} 颗电池；{len(visible_tests)} 条实验；数据库 {self.database.counts()['discharge_tests']} 条")
        self.selection_changed()

    def _load_catalog_tests(self, rows):
        """Attach test rows to their sample without making tests define the catalog."""
        self.sample_tests = {}
        sequence_groups = {}
        for row in rows:
            sample_id = row.get('_sample_id', row.get('sample_id'))
            row['_sample_id'] = sample_id
            self.sample_tests.setdefault(sample_id, []).append(row)
            sequence_groups.setdefault(str(sample_id), []).append(row)
        for group_rows in sequence_groups.values():
            ordered_rows = sorted(group_rows, key=lambda item: (str(item.get('test_date') or ''),
                                                                  int(item.get('id', 0) or 0)))
            for sequence, row in enumerate(ordered_rows, 1):
                row['_sequence'] = sequence

    def _load_preview_catalog(self):
        catalogs = {}
        rows = []
        for candidate in self.preview:
            metadata = dict(candidate.get('metadata') or {})
            summary = dict(candidate.get('summary') or {})
            row = dict(metadata)
            row.update(summary)
            row.update(source_file=candidate.get('source_file',''), source_sheet=candidate.get('source_sheet',''),
                       _candidate=candidate, _status=('待审查异常' if candidate.get('issues') else '待审查'))
            form_factor = row.get('form_factor') or ''
            category_code = row.get('category_code') or ''
            sample_code = row.get('sample_code') or ''
            category_id = f'preview:{form_factor}:{category_code}'
            sample_id = f'{category_id}:{sample_code}'
            row.update(_sample_id=sample_id, sample_id=sample_id, category_id=category_id)
            if sample_id not in catalogs:
                catalog = dict(metadata)
                catalog.update(id=sample_id, sample_id=sample_id, category_id=category_id,
                               form_factor=form_factor, category_code=category_code, sample_code=sample_code)
                catalogs[sample_id] = catalog
            rows.append(row)
        self.catalog = list(catalogs.values())
        self._load_catalog_tests(rows)

    def battery_changed(self, _event=None):
        selected = self.battery_table.selection()
        if selected and str(selected[0]) != str(self.selected_sample_id):
            self._load_sample(selected[0], select_first=True)

    def _load_sample(self, sample_id, select_first=True):
        sample_id = str(sample_id)
        sample = self.catalog_by_id.get(sample_id)
        if sample is None:
            sample = next((row for row in self.catalog if str(row.get('id')) == sample_id), None)
        self.selected_sample_id = sample_id
        self.current_sample = sample
        self.rows = list(self.sample_tests.get(sample_id, self.sample_tests.get(self.sample_tree_ids.get(sample_id), [])))
        self.listbox.delete(0, 'end')
        for row in self.rows:
            self.listbox.insert('end', test_name(row, row.get('_sequence', 1)))
        if select_first and self.rows:
            self.listbox.selection_set(0)
        self._render_catalog(sample)
        self.selection_changed()

    def selected(self):
        return [self.rows[i] for i in self.listbox.curselection()]

    def get_points(self,row):
        if '_candidate' in row:
            return row.get('_candidate',{}).get('points') or []
        test_id = row.get('id')
        if test_id in (None, ''):
            return []
        if test_id not in self.point_cache:
            self.point_cache[test_id] = self.database.points(test_id)
        return self.point_cache[test_id]

    def selection_changed(self):
        selected=self.selected();self.ax.clear();self.query_result.set('')
        plotted = False
        for row in selected:
            xy=[(p.get(self.x.get()),p.get(self.y.get())) for p in self.get_points(row)]
            # NaN deliberately breaks the line at missing measurements.
            if xy:
                xs=[float(a) if a is not None else math.nan for a,b in xy]
                ys=[float(b) if b is not None else math.nan for a,b in xy]
                self.ax.plot(xs,ys,label=f"{display_sample_code(row)} · 第{row.get('_sequence',1)}次")
                plotted = True
        self.ax.set_xlabel(POINT_FIELDS[self.x.get()]);self.ax.set_ylabel(POINT_FIELDS[self.y.get()]);self.ax.grid(alpha=.25)
        if plotted:self.ax.legend(fontsize=8)
        elif self.current_sample is not None and not any(self.get_points(row) for row in self.rows):
            self.ax.text(.5,.5,'暂无放电曲线\n该电池的完整资料仍可在“电池资料”页查看',ha='center',va='center',transform=self.ax.transAxes)
        else:self.ax.text(.5,.5,'选择实验查看曲线\n空库可通过上方按钮只读预览 Excel',ha='center',va='center',transform=self.ax.transAxes)
        self.figure.tight_layout();self.canvas.draw_idle()
        self.detail.configure(state='normal');self.detail.delete('1.0','end')
        self.table.delete(*self.table.get_children())
        for row in selected:
            invalid=not row.get('integration_valid',False)
            self.detail.insert('end',f"【{row.get('_status','待审查')}】{'容量与能量为暂算值，积分存在异常' if invalid else '积分有效；仍需审查样品与工况'}\n")
            for key, label in META_LABELS.items():
                value = row.get(key)
                if key in ('source_file', 'mapping_source') and value:
                    value = f'{resolve_source_path(value, "battery")}（原记录：{value}）'
                self.detail.insert('end', f'{label}：{shown(value)}\n')
            for issue in row.get('_candidate',{}).get('issues',[]):self.detail.insert('end',f'异常：{issue}\n')
            for warning in row.get('import_warnings',[]):self.detail.insert('end',f'说明：{warning}\n')
            self.detail.insert('end','\n')
        self.detail.configure(state='disabled')
        if selected:
            for i,p in enumerate(self.get_points(selected[0])):
                self.table.insert('', 'end',iid=str(i),values=[shown(p.get(k)) for k in self.table['columns']])
        self._update_action_state()

    def _update_action_state(self):
        selected = self.selected()
        export_row = selected[0] if len(selected) == 1 else None
        export_points = self.get_points(export_row) if export_row else []
        export_valid = (self.source.get() == '数据库' and export_row is not None
                        and export_row.get('review_status') == 'reviewed'
                        and export_row.get('integration_valid') is True
                        and len(export_points) >= 2)
        self.export_button.configure(state='normal' if export_valid else 'disabled')
        adopt_valid = (self.source.get() == '数据库' and bool(selected)
                       and all(row.get('review_status') == 'reviewed'
                               and row.get('id') not in (None, '')
                               and len(self.get_points(row)) >= 2 for row in selected))
        mark_state = 'normal' if adopt_valid else 'disabled'
        self.adopt.configure(state=mark_state);self.unadopt.configure(state=mark_state)

    def _build_catalog_page(self, parent):
        ttk.Label(parent, text='当前电池的汇总资料；参考测试来自汇总表，仅供查阅，容量和能量以放电曲线积分为准。',
                  foreground='#8a5200', padding=8).pack(fill='x')
        tabs = ttk.Notebook(parent)
        tabs.pack(fill='both', expand=True, padx=6, pady=(0, 6))

        info_frame = ttk.Frame(tabs, padding=5)
        tabs.add(info_frame, text='基本资料')
        info_box = ttk.Frame(info_frame); info_box.pack(fill='both', expand=True)
        self.catalog_info = tk.Text(info_box, wrap='word', font=('Microsoft YaHei UI', 10), height=9,
                                    padx=10, pady=8)
        info_scroll = ttk.Scrollbar(info_box, command=self.catalog_info.yview)
        self.catalog_info.configure(yscrollcommand=info_scroll.set)
        info_scroll.pack(side='right', fill='y'); self.catalog_info.pack(fill='both', expand=True)
        self.catalog_info.configure(state='disabled')

        reference_frame = ttk.Frame(tabs, padding=5)
        tabs.add(reference_frame, text='参考测试')
        reference_box = ttk.Frame(reference_frame); reference_box.pack(fill='both', expand=True)
        ref_columns = ('date', 'device', 'capacity_mah', 'energy_original', 'conditions', 'charge_conditions')
        self.catalog_reference_table = ttk.Treeview(reference_box, columns=ref_columns, show='headings')
        ref_heads = {'date':'日期', 'device':'设备', 'capacity_mah':'容量 (mAh)', 'energy_original':'能量（原表）',
                     'conditions':'放电条件', 'charge_conditions':'充电条件'}
        ref_widths = {'date':145, 'device':120, 'capacity_mah':105, 'energy_original':120,
                      'conditions':220, 'charge_conditions':220}
        for key in ref_columns:
            self.catalog_reference_table.heading(key, text=ref_heads[key])
            self.catalog_reference_table.column(key, width=ref_widths[key], stretch=False)
        rsy=ttk.Scrollbar(reference_box, command=self.catalog_reference_table.yview)
        rsx=ttk.Scrollbar(reference_box, orient='horizontal', command=self.catalog_reference_table.xview)
        self.catalog_reference_table.configure(yscrollcommand=rsy.set, xscrollcommand=rsx.set)
        rsy.pack(side='right', fill='y'); rsx.pack(side='bottom', fill='x')
        self.catalog_reference_table.pack(fill='both', expand=True)

        fields_frame = ttk.Frame(tabs, padding=5)
        tabs.add(fields_frame, text='原表字段')
        ttk.Label(fields_frame, text='保留原始列、单元格和表头上下文；跨行续表资料见最后一个页签。',
                  foreground='#666').pack(anchor='w', pady=(0, 5))
        fields_box = ttk.Frame(fields_frame); fields_box.pack(fill='both', expand=True)
        field_columns = ('column', 'cell', 'label', 'value', 'header_context')
        self.catalog_fields_table = ttk.Treeview(fields_box, columns=field_columns, show='headings')
        field_heads = {'column':'列', 'cell':'源单元格', 'label':'字段', 'value':'原值', 'header_context':'表头上下文'}
        field_widths = {'column':55, 'cell':90, 'label':150, 'value':300, 'header_context':300}
        for key in field_columns:
            self.catalog_fields_table.heading(key, text=field_heads[key])
            self.catalog_fields_table.column(key, width=field_widths[key], stretch=False)
        fsy=ttk.Scrollbar(fields_box, command=self.catalog_fields_table.yview)
        fsx=ttk.Scrollbar(fields_box, orient='horizontal', command=self.catalog_fields_table.xview)
        self.catalog_fields_table.configure(yscrollcommand=fsy.set, xscrollcommand=fsx.set)
        fsy.pack(side='right', fill='y'); fsx.pack(side='bottom', fill='x')
        self.catalog_fields_table.pack(fill='both', expand=True)

        continuation_frame = ttk.Frame(tabs, padding=5)
        tabs.add(continuation_frame, text='续行资料')
        ttk.Label(continuation_frame, text='按源行逐条保留续行字段原值，避免把长记录折叠成摘要。',
                  foreground='#666').pack(anchor='w', pady=(0, 5))
        continuation_box = ttk.Frame(continuation_frame); continuation_box.pack(fill='both', expand=True)
        self.catalog_continuation_table = ttk.Treeview(continuation_box, columns=('source_row','fields'), show='headings')
        self.catalog_continuation_table.heading('source_row', text='源行')
        self.catalog_continuation_table.heading('fields', text='续行字段原值')
        self.catalog_continuation_table.column('source_row', width=75, stretch=False)
        self.catalog_continuation_table.column('fields', width=620, stretch=False)
        csy=ttk.Scrollbar(continuation_box, command=self.catalog_continuation_table.yview)
        csx=ttk.Scrollbar(continuation_box, orient='horizontal', command=self.catalog_continuation_table.xview)
        self.catalog_continuation_table.configure(yscrollcommand=csy.set, xscrollcommand=csx.set)
        csy.pack(side='right', fill='y'); csx.pack(side='bottom', fill='x')
        self.catalog_continuation_table.pack(fill='both', expand=True)

    def _reference_rows(self, sample):
        references = sample.get('catalog_reference_tests') if sample else None
        if isinstance(references, list):
            # An explicit empty list means that the source catalog has no reference
            # tests; do not reinterpret a leakage/remark array as measurements.
            return [self._normalise_reference(item, index + 1) for index, item in enumerate(references)]
        # Older imports kept a flat array. Keep it readable and bounded by presenting
        # date-anchored records rather than putting the entire JSON value in the detail pane.
        raw = sample.get('summary_reference') if sample else None
        if isinstance(raw, list) and raw:
            result = []
            date_indices = [index for index, value in enumerate(raw)
                            if re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', shown(value))]
            for index in date_indices:
                chunk = raw[index:index + 5]
                if not chunk:
                    continue
                values = list(chunk) + [None] * (5 - len(chunk))
                result.append({'date':shown(values[0]), 'device':shown(values[1]),
                               'capacity_mah':shown(values[2]),
                               'energy_original':shown(values[3]) + ' 原单位' if values[3] not in (None, '') else '—',
                               'conditions':shown(values[4]), 'charge_conditions':'—'})
            return result
        return []

    def _normalise_reference(self, item, index):
        if not isinstance(item, dict):
            return {'date':'—', 'device':f'原汇总字段 {index}', 'capacity_mah':'—',
                    'energy_original':shown(item), 'conditions':'—', 'charge_conditions':'—'}
        date = item.get('date', item.get('test_date', item.get('test_date_text')))
        device = item.get('device', item.get('equipment', item.get('instrument')))
        capacity = item.get('capacity_mah', item.get('capacity_mAh', item.get('capacity')))
        if capacity in (None, '') and item.get('capacity_ah') not in (None, ''):
            try:
                capacity = float(item['capacity_ah']) * 1000
            except (TypeError, ValueError):
                capacity = item.get('capacity_ah')
        energy_value = item.get('energy_value')
        energy_unit = item.get('energy_unit')
        if energy_value in (None, ''):
            if item.get('energy_mwh') not in (None, ''):
                energy_value, energy_unit = item.get('energy_mwh'), energy_unit or 'mWh'
            elif item.get('energy_wh') not in (None, ''):
                energy_value, energy_unit = item.get('energy_wh'), energy_unit or 'Wh'
        energy = '—' if energy_value in (None, '') else f'{shown(energy_value)} {energy_unit or "原单位"}'
        if item.get('energy_note'):
            energy = f'{energy}（{shown(item["energy_note"])}）'
        return {'date':shown(date), 'device':shown(device), 'capacity_mah':shown(capacity),
                'energy_original':energy, 'conditions':shown(item.get('conditions', item.get('discharge_conditions'))),
                'charge_conditions':shown(item.get('charge_conditions', item.get('charging_conditions')))}

    def _render_catalog(self, sample):
        self.catalog_info.configure(state='normal'); self.catalog_info.delete('1.0', 'end')
        self.catalog_reference_table.delete(*self.catalog_reference_table.get_children())
        self.catalog_fields_table.delete(*self.catalog_fields_table.get_children())
        self.catalog_continuation_table.delete(*self.catalog_continuation_table.get_children())
        if not sample:
            self.catalog_info.insert('end', '请选择左侧电池查看完整资料。')
            self.catalog_info.configure(state='disabled')
            return
        self.catalog_info.insert('end', f'已收录 {len(self.rows)} 条实验\n\n' if self.rows else '暂无放电曲线\n\n')
        preferred_keys = ['form_factor', 'category_code', 'model_name', 'sample_code', 'summary_code',
                          'manufacturer', 'supplier', 'condition', 'nominal_capacity_ah', 'nominal_voltage_v',
                          'weight_g', 'model_spec', 'dimensions', 'appearance', 'purchase_date', 'rated_c_rate',
                          'ac_resistance_mohm', 'ac_resistance_voltage_v', 'ac_resistance_note', 'usage',
                          'mapping_source', 'mapping_sheet', 'mapping_row']
        implementation_keys = {'id', 'category_id', 'sample_id', 'mapping_hash', 'source_hash', 'fingerprint',
                               'source_file', 'source_sheet', 'preferred', 'review_status', 'repeat_key',
                               'catalog_fields', 'catalog_reference_tests', 'catalog_continuation_rows',
                               'summary_reference', 'summary_reference_start_column'}
        rendered = set()
        for key in preferred_keys + sorted(sample):
            if key in rendered or key in implementation_keys or key.startswith('_'):
                continue
            if key not in sample:
                continue
            rendered.add(key)
            value = sample.get(key)
            if key == 'mapping_source' and value:
                value = f'{resolve_source_path(value, "battery")}（原记录：{value}）'
            self.catalog_info.insert('end', f'{META_LABELS.get(key, key)}：{shown(value)}\n')
        self.catalog_info.configure(state='disabled')
        for row in self._reference_rows(sample):
            self.catalog_reference_table.insert('', 'end', values=[row.get(key, '—') for key in
                                                                     ('date','device','capacity_mah','energy_original',
                                                                      'conditions','charge_conditions')])
        fields = sample.get('catalog_fields') or []
        if isinstance(fields, list):
            for index, field in enumerate(fields):
                if not isinstance(field, dict):
                    field = {'value': field}
                header = field.get('header_context', '')
                if isinstance(header, (list, dict)):
                    header = json.dumps(header, ensure_ascii=False)
                self.catalog_fields_table.insert('', 'end', values=(shown(field.get('column')),
                    shown(field.get('cell')), shown(field.get('label', field.get('name'))),
                    shown(field.get('value')), shown(header)))
        continuation = sample.get('catalog_continuation_rows') or []
        if isinstance(continuation, list):
            for row in continuation:
                if not isinstance(row, dict):
                    self.catalog_continuation_table.insert('', 'end', values=('—', shown(row)))
                    continue
                fields = row.get('fields', row.get('values', {}))
                if isinstance(fields, dict):
                    text = '; '.join(f'{key}={shown(value)}' for key, value in fields.items())
                elif isinstance(fields, list):
                    parts = []
                    for value in fields:
                        if isinstance(value, dict):
                            key = value.get('column', value.get('cell', value.get('label', '')))
                            parts.append(f'{shown(key)}={shown(value.get("value"))}')
                        else:
                            parts.append(shown(value))
                    text = '; '.join(parts)
                else:
                    text = shown(fields)
                self.catalog_continuation_table.insert('', 'end', values=(shown(row.get('source_row')), text))

    def export_simulation(self):
        if self.source.get() != '数据库':
            messagebox.showinfo('导出仿真曲线', 'Excel 预览不能导出仿真曲线，请先切换到数据库。', parent=self.root)
            return
        selected = self.selected()
        if len(selected) != 1:
            messagebox.showinfo('导出仿真曲线', '请恰好选择一条有完整采样点的实验。', parent=self.root)
            return
        row = selected[0]
        sample_id, test_id = row.get('_sample_id', row.get('sample_id')), row.get('id')
        if (sample_id in (None, '') or test_id in (None, '')
                or row.get('review_status') != 'reviewed'
                or row.get('integration_valid') is not True
                or len(self.get_points(row)) < 2):
            messagebox.showinfo('导出仿真曲线', '当前选择不是可导出的数据库实验。', parent=self.root)
            return
        try:
            from .selection import BatterySelection
        except ImportError as exc:
            messagebox.showerror('导出失败', f'仿真接口不可用：{exc}', parent=self.root)
            return
        try:
            with BatterySelection(self.database.path) as selector:
                payload = selector.select_curve(int(sample_id), test_id=int(test_id))
            if not isinstance(payload, dict) or payload.get('status') != 'ready':
                message = payload.get('message') if isinstance(payload, dict) else None
                raise ValueError(message or '仿真接口未返回可导出的 ready 曲线')
            selection = payload.get('selection') or {}
            if str(selection.get('sample_id')) != str(sample_id) or str(selection.get('test_id')) != str(test_id):
                raise ValueError('接口返回的样品或实验与当前选择不一致')
            curve = payload.get('curve') or {}
            if not isinstance(curve.get('points'), list) or len(curve['points']) < 2:
                raise ValueError('接口返回的曲线没有完整采样点')
            out_dir = self.database.path.parent / 'simulation_exports'
            out_dir.mkdir(parents=True, exist_ok=True)
            form_factor = safe_filename(row.get('form_factor') or '未知类型')
            output = out_dir / (f'{form_factor}_sample-{safe_filename(sample_id)}_'
                                f'test-{safe_filename(test_id)}.json')
            temporary = output.with_suffix(output.suffix + '.tmp')
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
                temporary.replace(output)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except Exception as exc:
            messagebox.showerror('导出失败', str(exc), parent=self.root)
            return
        self.status.set(f'仿真曲线已导出：{output}')
        self._show_export_success(output)

    def _show_export_success(self, output):
        window = tk.Toplevel(self.root); window.title('导出完成'); window.resizable(True, False)
        ttk.Label(window, text='仿真曲线已成功导出：', padding=(12, 10, 12, 4)).pack(anchor='w')
        path_var = tk.StringVar(value=str(output))
        path_entry = ttk.Entry(window, textvariable=path_var, state='readonly', width=100)
        path_entry.pack(fill='x', padx=12, pady=4)
        actions = ttk.Frame(window); actions.pack(fill='x', padx=12, pady=(4, 10))
        def open_folder():
            try:
                if hasattr(os, 'startfile'):
                    os.startfile(str(output.parent))
                else:
                    raise OSError('当前系统不支持打开文件夹')
            except OSError as exc:
                messagebox.showerror('打开文件夹失败', str(exc), parent=window)
        ttk.Button(actions, text='打开所在文件夹', command=open_folder).pack(side='left')
        ttk.Button(actions, text='关闭', command=window.destroy).pack(side='right')

    def query_point(self):
        try:
            t=float(self.query_time.get())
            if not math.isfinite(t):raise ValueError()
            selected=self.selected()
            if not selected:raise ValueError('请先选择实验')
            points=self.get_points(selected[0]);valid=[(i,p) for i,p in enumerate(points) if p.get('elapsed_s') is not None]
            if not valid:raise ValueError('没有有效时间点')
            i,p=min(valid,key=lambda pair:abs(pair[1]['elapsed_s']-t))
            self.table.selection_set(str(i));self.table.see(str(i))
            self.query_result.set(f"最近实测点：{shown(p['elapsed_s'])} s，{shown(p['voltage_v'])} V；未插值")
        except ValueError as exc:messagebox.showerror('查询',str(exc) or '请输入有效秒数',parent=self.root)

    def mark(self,preferred):
        if self.source.get()!='数据库':return
        ids=[r['id'] for r in self.selected()]
        if not ids:return
        try:self.database.set_preferred(ids,preferred);self.reload()
        except Exception as exc:messagebox.showerror('标记失败',str(exc),parent=self.root)

    def choose_mappings(self):
        paths=filedialog.askopenfilenames(title='选择型号与来源汇总表',filetypes=[('Excel','*.xlsx')],parent=self.root)
        if paths:
            self.mapping_paths=[Path(p) for p in paths]
            self.status.set(f'已选择 {len(paths)} 份汇总表；重新预览实验文件后生效')

    def show_catalog(self):
        window=tk.Toplevel(self.root);window.title('电池资料 · 汇总表原始信息');window.geometry('1180x650')
        ttk.Label(window,text='这些是电池资料；汇总表后续测试结果仅供参考，容量与能量以放电曲线积分为准。',padding=8).pack(fill='x')
        rows=self.database.samples()
        search=tk.StringVar()
        entry=ttk.Entry(window,textvariable=search);entry.pack(fill='x',padx=8,pady=4)
        columns=['form_factor','summary_code','appearance','purchase_date','manufacturer','supplier','condition',
            'nominal_capacity_ah','rated_c_rate','ac_resistance_mohm','ac_resistance_voltage_v','usage']
        frame=ttk.Frame(window);frame.pack(fill='both',expand=True,padx=8,pady=8)
        table=ttk.Treeview(frame,columns=columns,show='headings')
        for k in columns:
            table.heading(k,text=META_LABELS[k]);table.column(k,width=150,stretch=False)
        sy=ttk.Scrollbar(frame,orient='vertical',command=table.yview)
        sx=ttk.Scrollbar(frame,orient='horizontal',command=table.xview)
        table.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        sy.pack(side='right',fill='y');sx.pack(side='bottom',fill='x');table.pack(fill='both',expand=True)
        def refresh(*_):
            table.delete(*table.get_children())
            term=search.get().casefold()
            for row in rows:
                if term in json.dumps(row,ensure_ascii=False).casefold():
                    table.insert('','end',values=[shown(row.get(k)) for k in columns])
        search.trace_add('write',refresh);refresh()

    def open_preview(self):
        if self.busy:return
        paths=filedialog.askopenfilenames(title='只读预览放电实验',filetypes=[('放电原始记录','*.xlsx *.csv')],parent=self.root)
        if not paths:return
        self.busy=True;self.open_button.configure(state='disabled');self.status.set('正在只读解析，未导入数据…')
        mappings=list(self.mapping_paths)
        def work():
            try:self.results.put((preview_workbooks(paths,mappings),None))
            except Exception as exc:self.results.put((None,str(exc)))
        threading.Thread(target=work,daemon=True).start();self.poll_id=self.root.after(100,self.poll)

    def poll(self):
        if self.closed:return
        try:result,error=self.results.get_nowait()
        except queue.Empty:self.poll_id=self.root.after(100,self.poll);return
        self.poll_id=None;self.busy=False;self.open_button.configure(state='normal')
        if error:messagebox.showerror('预览失败',error,parent=self.root);return
        self.preview=result;self.source.set('Excel 预览');self.clear_filters()
        if self.rows:self.listbox.selection_set(0);self.selection_changed()
        self.notebook.select(1)

    def close(self):
        state=dict(version=2, geometry=self.root.geometry(),x=self.x.get(),y=self.y.get(),
            filters={k:v.get() for k,v in self.filters.items()},sash=self.panes.sashpos(0),
            selected_sample_id=self.selected_sample_id,
            selected_ids=[r['id'] for r in self.selected() if 'id' in r],
            widths={k:self.table.column(k,'width') for k in self.table['columns']})
        try:
            tmp=self.state_path.with_suffix('.tmp');tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(self.state_path)
        except OSError as exc:
            messagebox.showerror('界面状态保存失败',str(exc),parent=self.root)
            return
        self.closed=True
        if self.poll_id:self.root.after_cancel(self.poll_id)
        self.root.destroy()
