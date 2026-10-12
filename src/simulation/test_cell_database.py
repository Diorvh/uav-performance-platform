"""单电芯数据库适配用例；临时源库独立于用户实测数据库。"""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from .battery_library import curve_from_record
from .cell_database import load_cell_test, verify_cell_source


class CellDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'cell.sqlite3'
        con = sqlite3.connect(self.path)
        with con:
            con.executescript('''PRAGMA user_version=2;
CREATE TABLE categories(id,form_factor,category_code);
CREATE TABLE samples(id,category_id,sample_code,metadata_json);
CREATE TABLE discharge_tests(id,sample_id,test_date,metadata_json,summary_json,preferred,review_status,cutoff_voltage_v,set_current_a,source_file);
CREATE TABLE discharge_points(id,test_id,point_order,elapsed_s,voltage_v,current_a,source_row);
INSERT INTO categories VALUES(1,'21700','12');
INSERT INTO samples VALUES(1,1,'2','{"model_name":"test"}');
''')
            meta = json.dumps({'mode': '0', 'max_gap_s': 4000})
            con.execute('INSERT INTO discharge_tests VALUES(1,1,?,?,?,?,?,?,?,?)',
                        ('date', meta, '{"integration_valid":true}', 1, 'reviewed', 2.5, 1.0, 'test.csv'))
            con.executemany('INSERT INTO discharge_points VALUES(?,1,?,?,?,?,?)',
                            [(1,1,0,4.2,0,7), (2,2,1,4.19,1,8), (3,3,1801,3.6,1.02,9), (4,4,3601,2.5,1,10)])
        con.close()

    def test_measured_capacity_and_read_only_source(self):
        before = self.path.read_bytes()
        record = load_cell_test(self.path, 1, 1)
        curve = curve_from_record(record)
        self.assertAlmostEqual(curve.usable_capacity_ah, 1.01)
        self.assertEqual(record['source']['trimmed_start_count'], 1)
        self.assertEqual(curve.start_voltage_v, 4.19)
        self.assertEqual(self.path.read_bytes(), before)
        verify_cell_source(record)

    def test_explicit_selection_does_not_change_adoption(self):
        con = sqlite3.connect(self.path)
        with con:
            con.execute('UPDATE discharge_tests SET preferred=0')
        con.close()
        before = self.path.read_bytes()
        record = load_cell_test(self.path, 1, 1)
        self.assertEqual(record['source']['selection_basis'], 'explicit cell and experiment selection')
        self.assertEqual(before, self.path.read_bytes())

    def test_no_curve_and_wrong_cell_are_rejected(self):
        con = sqlite3.connect(self.path)
        with con:
            con.execute("INSERT INTO samples VALUES(2,1,'3','{}')")
        con.close()
        with self.assertRaisesRegex(ValueError, '没有可用于仿真'):
            load_cell_test(self.path, 2)
        with self.assertRaisesRegex(ValueError, '不属于'):
            load_cell_test(self.path, 2, 1)

    def test_multiple_tests_require_choice(self):
        con = sqlite3.connect(self.path)
        with con:
            con.execute('INSERT INTO discharge_tests SELECT 2,sample_id,test_date,metadata_json,summary_json,preferred,review_status,cutoff_voltage_v,set_current_a,source_file FROM discharge_tests')
            con.execute('INSERT INTO discharge_points SELECT id+10,2,point_order,elapsed_s,voltage_v,current_a,source_row FROM discharge_points WHERE test_id=1')
        con.close()
        with self.assertRaisesRegex(ValueError, '多条可用曲线'):
            load_cell_test(self.path, 1)
        self.assertEqual(load_cell_test(self.path, 1, 2)['source']['test_id'], 2)

    def test_cutoff_mismatch_and_stale_source(self):
        old = load_cell_test(self.path, 1, 1)
        con = sqlite3.connect(self.path)
        with con:
            con.execute('UPDATE discharge_points SET voltage_v=2.59 WHERE id=4')
        con.close()
        with self.assertRaises(ValueError):
            verify_cell_source(old)
        with self.assertRaises(ValueError):
            load_cell_test(self.path, 1, 1)
        current = load_cell_test(self.path, 1, 1, allow_partial=True)
        self.assertTrue(current['source']['partial_window'])
        self.assertEqual(curve_from_record(current).end_voltage_v, 2.59)

    def dense_curve(self, changed):
        con = sqlite3.connect(self.path)
        with con:
            con.execute('DELETE FROM discharge_points')
            con.executemany('INSERT INTO discharge_points VALUES(?,1,?,?,?,?,?)',
                            [(i+1,i+1,i,4.2-1.7*i/1000,changed.get(i,1.0),i+7) for i in range(1001)])
        con.close()

    def test_short_ripple_requires_explicit_approximation_and_keeps_points(self):
        self.dense_curve({500:1.2})
        with self.assertRaisesRegex(ValueError, '短时偏离'):
            load_cell_test(self.path, 1, 1)
        record = load_cell_test(self.path, 1, 1, allow_ripple=True)
        self.assertTrue(record['source']['approximate_current'])
        self.assertEqual(len(record['points']), 1001)
        self.assertAlmostEqual(curve_from_record(record).usable_capacity_ah, 1000.2/3600)

    def test_stop_and_sustained_current_change_are_not_waived(self):
        for changes in ({500:0}, {i:1.2 for i in range(400,430)}):
            self.dense_curve(changes)
            with self.assertRaisesRegex(ValueError, '停流或超出'):
                load_cell_test(self.path, 1, 1, allow_ripple=True, allow_partial=True)


if __name__ == '__main__':
    unittest.main()
