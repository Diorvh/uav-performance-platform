from __future__ import annotations

import argparse
import json
from pathlib import Path

from .database import Database

from platform_config import project_path


def main() -> None:
    parser = argparse.ArgumentParser(description='电池放电数据库：独立数据库与只读导入预览')
    parser.add_argument('--database', type=Path, default=project_path('battery_database'))
    parser.add_argument('--preview', nargs='+', type=Path, help='只读预览 Excel，不连接数据库')
    parser.add_argument('--mapping', nargs='*', type=Path, default=[])
    parser.add_argument('--init-only', action='store_true', help='仅初始化空数据库结构')
    args = parser.parse_args()
    if args.preview:
        from .excel_import import preview_workbooks
        candidates = preview_workbooks(args.preview, mapping_paths=args.mapping)
        for candidate in candidates:
            report = {k: v for k, v in candidate.items() if k != 'points'}
            report['point_count'] = len(candidate.get('points', []))
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    if args.init_only:
        Database.initialize(args.database)
        return
    if not args.database.is_file():
        raise FileNotFoundError(f'数据库不存在：{args.database}')
    import tkinter as tk
    from .gui import App
    database = Database(args.database)
    try:
        root = tk.Tk()
        App(root, database)
        root.mainloop()
    finally:
        database.close()


if __name__ == '__main__':
    main()
