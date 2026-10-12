from __future__ import annotations

import argparse
import json
import sqlite3
import tkinter as tk
from pathlib import Path

if __package__ in {None, ""}:
    # 兼容 VSCode“运行当前文件”：此时 main.py 没有父包上下文。
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from force_efficiency.database import Database, SCHEMA
    from force_efficiency.excel_import import DEFAULT_SOURCE_FILES, import_preview
    from force_efficiency.gui import App
else:
    from .database import Database, SCHEMA
    from .excel_import import DEFAULT_SOURCE_FILES, import_preview
    from .gui import App


from platform_config import project_path


def self_check() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    group_id = connection.execute(
        """INSERT INTO test_groups
        (display_order, motor_spec, propeller, nominal_voltage_v, source_file, source_workbook_index,
         source_sheet, source_sheet_index, source_block_index, source_anchor)
        VALUES (1, '2306-1850kV', '7040-3GF', 7.4, 'sample.xlsx', 1, 'Sheet1', 1, 1, 'B3')"""
    ).lastrowid
    connection.execute(
        "INSERT INTO measurements (group_id, point_order, throttle_us, thrust_g) VALUES (?, 1, 1200, 25)",
        (group_id,),
    )
    assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 1
    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="电机桨叶力效数据库查看器")
    parser.add_argument("database", nargs="?", default=project_path("force_database"))
    parser.add_argument("--preview-import", action="store_true", help="只扫描 Excel 并输出待审查清单")
    parser.add_argument("--source", nargs="*", type=Path, default=DEFAULT_SOURCE_FILES)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.preview_import:
        print(json.dumps(import_preview(args.source), ensure_ascii=False, indent=2))
        return
    if not Path(args.database).is_file():
        raise FileNotFoundError(f"数据库不存在：{args.database}")
    root = tk.Tk()
    App(root, Database(args.database))
    root.mainloop()


if __name__ == "__main__":
    main()
