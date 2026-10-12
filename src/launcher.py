"""One launch point for the three independently usable desktop modules."""
import argparse
from datetime import datetime
import subprocess
import sys
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

from platform_config import ROOT, project_path, require_database


MODULES = {'force': '力效数据库', 'battery': '电池数据库', 'simulation': '悬停续航仿真'}


def run_module(name):
    if name == 'force':
        from force_efficiency.database import Database
        from force_efficiency.gui import App
        database = Database(require_database('force'))
        root = tk.Tk()
        try:
            App(root, database)
            root.mainloop()
        finally:
            database.close()
    elif name == 'battery':
        from battery_discharge.database import Database
        from battery_discharge.gui import App
        database = Database(require_database('battery'))
        root = tk.Tk()
        try:
            App(root, database)
            root.mainloop()
        finally:
            database.close()
    else:
        from simulation.gui import main
        main()


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('无人机性能平台')
        self.geometry('660x450')
        self.minsize(610, 420)
        self.children_processes = {}
        body = ttk.Frame(self, padding=24)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='无人机性能平台', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(body, text='管理实验数据，选择电芯与动力配置，构建续航模型。').pack(anchor='w', pady=(8, 20))
        descriptions = {'force': '查看电机、桨叶与电调实验，管理采用的实验组。',
                        'battery': '按类型、型号、具体电芯查看资料与放电曲线。',
                        'simulation': '选择力效实验及电芯曲线，保存电池组并计算续航。'}
        for key, label in MODULES.items():
            row = ttk.Frame(body)
            row.pack(fill='x', pady=9)
            ttk.Button(row, text=label, width=19, command=lambda k=key: self.open_module(k)).pack(side='left')
            ttk.Label(row, text=descriptions[key], wraplength=370).pack(side='left', padx=16)
        ttk.Separator(body).pack(fill='x', pady=18)
        ttk.Label(body, text=f'当前项目：{ROOT}\n数据库与原始资料统一保存在本项目目录。',
                  wraplength=600, foreground='#555555').pack(anchor='w')
        self.status = tk.StringVar(value='电池组入口位于仿真窗口的“电池组模型库”。')
        ttk.Label(body, textvariable=self.status, wraplength=590).pack(anchor='w', pady=12)

    def open_module(self, name):
        previous = self.children_processes.get(name)
        if previous is not None and previous.poll() is None:
            self.status.set(f'{MODULES[name]}已打开，请切换到该窗口。')
            return
        try:
            if name in ('force', 'battery'):
                require_database(name)
            logs = project_path('outputs') / 'logs'
            logs.mkdir(parents=True, exist_ok=True)
            log = logs / f'{name}_{datetime.now():%Y%m%d_%H%M%S}.log'
            with log.open('wb') as stream:
                process = subprocess.Popen([sys.executable, str(ROOT/'launch.py'), '--module', name],
                                           cwd=ROOT, stdout=stream, stderr=stream,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.children_processes[name] = process
            self.status.set(f'已打开{MODULES[name]}。关闭启动页不会中断正在使用的窗口。')
        except (ValueError, OSError) as exc:
            messagebox.showerror('无法启动', str(exc), parent=self)


def main():
    parser = argparse.ArgumentParser(description='无人机性能平台')
    parser.add_argument('--module', choices=MODULES)
    args = parser.parse_args()
    try:
        if args.module:
            run_module(args.module)
        else:
            Launcher().mainloop()
    except Exception as exc:
        if sys.stderr is not None:
            traceback.print_exc()
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('启动失败', str(exc), parent=root)
        root.destroy()
        raise SystemExit(1) from exc
