"""本地电池组模型库；与未来只读电芯实验数据库完全分开。

单电芯曲线快照按内容编号，电池组只存引用。只在用户保存时创建库和写入。
未来数据库适配器只需生成同样的来源记录，无需改动串并联算法。
"""

from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .battery_model import CurvePoint, DischargeCurve
from .battery_pack import BatteryPack


from platform_config import project_path

DEFAULT_LIBRARY = project_path("pack_library")


def cell_record(curve, name, source):
    """source 可以包含 CSV 路径或未来的数据库/实验 ID；不虚构实验字段。"""
    return {"scope": "single_cell", "name": name,
            "reference_current_a": curve.reference_current_a,
            "points": [asdict(p) for p in curve.points], "source": source}


def curve_from_record(record):
    if not isinstance(record, dict) or record.get("scope") != "single_cell":
        raise ValueError("电池组模型只接受明确标为单电芯的放电曲线。")
    if not isinstance(record.get("name"), str) or not record["name"].strip() or not isinstance(record.get("source"), dict):
        raise ValueError("电芯记录需提供型号名称和来源信息。")
    try:
        return DischargeCurve([CurvePoint(**p) for p in record["points"]], record["reference_current_a"])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"电芯记录字段不完整或类型错误：{exc}") from exc


def record_id(record):
    curve_from_record(record)
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


class BatteryLibrary:
    def __init__(self, path=DEFAULT_LIBRARY):
        self.path = Path(path).resolve()

    def load(self):
        """返回配置与曲线快照；损坏/不兼容的库必须报错，不当作空库覆盖。"""
        if not self.path.exists():
            return {}, {}
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as con:
                con.execute("BEGIN")
                if con.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("电池组模型库版本不受支持。")
                sources = {key: json.loads(payload) for key, payload in con.execute("SELECT id,payload FROM cell_sources")}
                packs = {key: (BatteryPack(**json.loads(payload)), revision)
                         for key, payload, revision in con.execute("SELECT id,payload,revision FROM packs ORDER BY name")}
            for key, record in sources.items():
                if record_id(record) != key:
                    raise ValueError("电芯曲线快照内容校验不一致。")
            if any(p.cell_data_id is not None and p.cell_data_id not in sources for p, _ in packs.values()):
                raise ValueError("电池组引用的电芯曲线不存在。")
            return packs, sources
        except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取电池组模型库：{exc}") from exc

    def save(self, profile, *, key=None, revision=None, source=None):
        """同名新建拒绝；编辑使用版本校验，防止其他窗口的保存被覆盖。"""
        if source is not None and record_id(source) != profile.cell_data_id:
            raise ValueError("电芯曲线引用与准备保存的快照不一致。")
        # 先验证既有文件，不能在错误的库中自动建表。
        _, existing_sources = self.load()
        if profile.cell_data_id is not None and source is None and profile.cell_data_id not in existing_sources:
            raise ValueError("引用的电芯曲线尚未保存。")
        selected_source = source if source is not None else existing_sources.get(profile.cell_data_id)
        if selected_source is not None and selected_source["name"] != profile.cell_name:
            raise ValueError("电芯型号与引用曲线不一致。")
        key = key or str(uuid.uuid4())
        try:
            with closing(sqlite3.connect(self.path, timeout=0.3)) as con, con:
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("BEGIN IMMEDIATE")
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    con.execute("CREATE TABLE cell_sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                    con.execute("CREATE TABLE packs(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                                "payload TEXT NOT NULL,revision INTEGER NOT NULL,cell_id TEXT REFERENCES cell_sources(id))")
                    con.execute("PRAGMA user_version=1")
                elif version != 1:
                    raise ValueError("电池组模型库版本已变化，请重新打开。")
                if source is not None:
                    con.execute("INSERT OR IGNORE INTO cell_sources VALUES (?,?)",
                                (profile.cell_data_id, json.dumps(source, ensure_ascii=False, allow_nan=False)))
                payload = json.dumps(asdict(profile), ensure_ascii=False, allow_nan=False)
                if revision is None:
                    con.execute("INSERT INTO packs VALUES (?,?,?,?,?)", (key, profile.name, payload, 1, profile.cell_data_id))
                    revision = 1
                else:
                    updated = con.execute("UPDATE packs SET name=?,payload=?,revision=revision+1,cell_id=? WHERE id=? AND revision=?",
                                          (profile.name, payload, profile.cell_data_id, key, revision))
                    if updated.rowcount != 1:
                        raise ValueError("该电池组已被其他窗口修改，请重新打开后编辑。")
                    revision += 1
            return key, revision
        except sqlite3.IntegrityError as exc:
            raise ValueError("电池组名称已存在，或引用的电芯曲线无效；请更名或选择已有模型编辑。") from exc
        except sqlite3.Error as exc:
            raise ValueError(f"保存失败：{exc}。若文件正被占用，请关闭占用程序后重试。") from exc
