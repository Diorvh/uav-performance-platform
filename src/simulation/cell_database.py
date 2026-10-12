"""电芯实验数据库只读适配；只选择单节、单次实验，不平均或改采用标记。"""

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

from .battery_model import CurvePoint, DischargeCurve
from .battery_library import cell_record
from battery_discharge.selection import BatterySelection
from platform_config import project_path, database_id, locate_database

DEFAULT_CELL_DATABASE = project_path('battery_database')


def read_cell_selection(path, sample_id, test_id=None):
    """Use the catalog-owned API; it enforces test ownership and review state."""
    try:
        with BatterySelection(path) as selection:
            response = selection.select_curve(sample_id, test_id)
        if response['status'] != 'ready':
            raise ValueError(response['message'])
        cell = dict(response['sample'])
        for key in ('curve_count', 'available_curve_count', 'has_curve', 'simulation_available'):
            cell.pop(key, None)
        test = dict(response['curve'])
        points = test.pop('points')
        digest = hashlib.sha256(json.dumps([cell, test, points], sort_keys=True,
                                           ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
        return cell, test, points, digest
    except sqlite3.Error as exc:
        raise ValueError(f'无法读取电芯数据库：{exc}') from exc


def current_window(raw):
    """Return stable endpoints and time-weighted current-ripple diagnostics.

    Bounds are explicit model eligibility rules, not claims about cell accuracy.
    No interior point is removed or replaced.
    """
    if len(raw) < 2:
        raise ValueError('实验采样点不足。')
    for point in raw:
        if any(not isinstance(point.get(key), (int, float)) or not math.isfinite(point[key])
               for key in ('elapsed_s', 'voltage_v', 'current_a')):
            raise ValueError('采样包含缺失或非有限数值。')
        if point['voltage_v'] <= 0 or point['current_a'] < 0:
            raise ValueError('采样包含非正电压或充电阶段。')
    if any(b['elapsed_s'] <= a['elapsed_s'] for a, b in zip(raw, raw[1:])):
        raise ValueError('采样时间未严格递增，不能重排后继续建模。')
    positive = [p['current_a'] for p in raw if p['current_a'] > 0]
    if not positive:
        raise ValueError('没有有效放电电流。')
    reference = statistics.median(positive)
    tolerance = max(.02, .05 * reference)
    valid = [i for i, p in enumerate(raw) if abs(p['current_a'] - reference) <= tolerance + 1e-12]
    if len(valid) < 2:
        raise ValueError('稳定恒流采样不足。')
    start, end = valid[0], valid[-1]
    points = raw[start:end + 1]
    flags = [abs(p['current_a'] - reference) > tolerance + 1e-12 for p in points]
    durations = [(points[min(len(points)-1, i+1)]['elapsed_s'] - points[max(0, i-1)]['elapsed_s']) / 2
                 for i in range(len(points))]
    total = points[-1]['elapsed_s'] - points[0]['elapsed_s']
    run = longest = 0.0
    for flag, duration in zip(flags, durations):
        run = run + duration if flag else 0.0
        longest = max(longest, run)
    stats = {'outside_tolerance_count': sum(flags),
             'outside_time_fraction': sum(dt for flag, dt in zip(flags, durations) if flag) / total,
             'longest_outside_s': longest,
             'max_relative_deviation': max(abs(p['current_a']-reference) for p in points) / reference,
             'contains_stop': any(p['current_a'] <= 0 for p in points),
             'limits': {'outside_time_fraction': .01, 'longest_outside_s': 10.0, 'max_relative_deviation': .5}}
    return start, end, reference, tolerance, stats


def load_cell_test(path, sample_id, test_id=None, *, allow_partial=False, allow_ripple=False):
    """稳定段按实测电流梯形积分；等效参考时间=q/Iref 适配现有容量查表。

    不把原始时间改写进数据库。转换时间不是原始采样时间，完整原始点随来源保存。
    起停段只从两端剔除；中间变流/大缺口拒绝，避免静默跨越实验阶段。
    """
    try:
        cell, test, raw, digest = read_cell_selection(path, sample_id, test_id)
        test_id = test['test_id']
        meta, summary = test['metadata'], test['summary']
        if summary.get('integration_valid') is not True:
            raise ValueError('源实验的积分有效性未通过，不能用于模型。')
        if str(meta.get('mode')) != '0':
            raise ValueError('当前模型仅支持已确认的恒流模式（mode=0）。')
        start, end, reference, tolerance, ripple = current_window(raw)
        points = raw[start:end + 1]
        if ripple['contains_stop'] or any(ripple[k] > v + 1e-12 for k, v in ripple['limits'].items()):
            raise ValueError('放电段存在停流或超出近似范围的电流变化，不能用单参考电流模型。')
        if ripple['outside_tolerance_count'] and not allow_ripple:
            raise ValueError(f"电流短时偏离参考值：{ripple['outside_tolerance_count']} 点，约占 {100*ripple['outside_time_fraction']:.3f}% 时长；请明确勾选短时波动近似，或换选实验。")
        gaps = [b['elapsed_s'] - a['elapsed_s'] for a, b in zip(points, points[1:])]
        max_gap = meta.get('max_gap_s') or max(10.0, statistics.median(gaps) * 5)
        if not isinstance(max_gap, (int, float)) or not math.isfinite(max_gap) or max_gap <= 0 or max(gaps) > max_gap:
            raise ValueError('放电段存在超出记录阈值的时间缺口。')
        cutoff = test['cutoff_voltage_v']
        if cutoff is not None and (not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff) or cutoff <= 0):
            raise ValueError('截止设定无效。')
        delta = points[-1]['voltage_v'] - cutoff if cutoff is not None else None
        partial = delta is None or delta > .05 or delta < -.05
        if partial and not allow_partial:
            raise ValueError('截止设定未知或稳定段末端与设定相差超过 0.05 V；请明确选择仅用已记录窗口，或换选完整实验。')
        charge = 0.0
        converted = [CurvePoint(0.0, points[0]['voltage_v'])]
        for previous, point in zip(points, points[1:]):
            charge += (previous['current_a'] + point['current_a']) / 2 * (point['elapsed_s'] - previous['elapsed_s']) / 3600
            converted.append(CurvePoint(charge * 3600 / reference, point['voltage_v']))
        curve = DischargeCurve(converted, reference)
        name = f"{cell['form_factor']} {cell['category_code']}-{cell['sample_code']} | {cell.get('model_name') or '型号未记录'}"
        source = {'type': 'single-cell SQLite', 'database_path': str(Path(path).resolve()),
                  'test_id': test_id, 'sample_id': sample_id, 'selection_sha256': digest,
                  'selection_version': 2, 'database_id': database_id('battery', path),
                  'selection_basis': 'explicit cell and experiment selection',
                  'allow_partial': allow_partial, 'partial_window': partial,
                  'allow_ripple': allow_ripple, 'current_ripple': ripple,
                  'approximate_current': bool(ripple['outside_tolerance_count']),
                  'cutoff_voltage_v': cutoff, 'cutoff_voltage_source': meta.get('cutoff_voltage_source', '未记录'),
                  'stable_end_voltage_v': points[-1]['voltage_v'],
                  'raw_end_voltage_v': raw[-1]['voltage_v'], 'end_minus_cutoff_v': delta,
                  'trimmed_start_count': start, 'trimmed_end_count': len(raw) - end - 1,
                  'time_basis': 'equivalent reference time from measured-current trapezoid integral',
                  'stable_current_tolerance_a': tolerance, 'test': test, 'raw_points': raw,
                  'cell_metadata': cell}
        return cell_record(curve, name, source)
    except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'电芯数据库数据不兼容：{exc}') from exc


def verify_cell_source(record):
    """保存、载入仿真时检查所选实验，已删除或修改的实验要求重新选择。"""
    source = record['source']
    if source.get('type') != 'single-cell SQLite':
        return
    if source.get('selection_version') != 2:
        raise ValueError('旧版电芯关联需要从当前电芯目录重新选择，以核对电芯身份。')
    path = locate_database('battery', source.get('database_id'), source['database_path'])
    _, _, _, digest = read_cell_selection(path, source['sample_id'], source['test_id'])
    if digest != source['selection_sha256']:
        raise ValueError('所选电芯资料或实验已变化，请重新选择并保存电池组。')
