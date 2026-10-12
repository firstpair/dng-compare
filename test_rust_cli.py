"""Exercise Rust verdicts on generated TIFF/DNG fixtures, never photo trees."""
import json
import pathlib
import subprocess
import tempfile
import unittest

import numpy as np
import tifffile


BINARY = pathlib.Path(__file__).parent / 'rust-scanner/target/release/dng-subset'


class RustVerdicts(unittest.TestCase):
    def run_scan(self, left, right, report, *options):
        result = subprocess.run([str(BINARY), str(left), str(right), str(report),
                                 '--rate-mib', '512', *options],
                                capture_output=True, text=True)
        return result, json.loads((report / 'summary.json').read_text())

    def write(self, path, delta=0):
        tifffile.imwrite(path, np.arange(256, dtype=np.uint16).reshape(16, 16) + delta,
                         metadata=None,
                         extratags=[(50706, 'B', 4, (1, 4, 0, 0), False)])

    def test_complete_equal_and_validated_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            left, right = root / 'left', root / 'right'
            left.mkdir(); right.mkdir()
            self.write(left / 'same.DNG'); self.write(right / 'same.DNG')
            first, summary = self.run_scan(left, right, root / 'report')
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(summary['left_subsumed_by_right'])
            resumed, summary = self.run_scan(left, right, root / 'report')
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(summary['newly_checked'], 0)

    def test_changed_pixels_complete_but_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            left, right = root / 'left', root / 'right'
            left.mkdir(); right.mkdir()
            self.write(left / 'same.DNG'); self.write(right / 'same.DNG', 1)
            result, summary = self.run_scan(left, right, root / 'report')
            self.assertEqual(result.returncode, 1)
            self.assertTrue(summary['complete'])
            self.assertFalse(summary['left_subsumed_by_right'])
            self.assertEqual(summary['unverified'], 1)

    def test_missing_candidate_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            left, right = root / 'left', root / 'right'
            left.mkdir(); right.mkdir(); self.write(left / 'absent.DNG')
            result, summary = self.run_scan(left, right, root / 'report')
            self.assertEqual(result.returncode, 1)
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['missing_name'], 1)

    def test_partial_pilot_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            left, right = root / 'left', root / 'right'
            left.mkdir(); right.mkdir()
            for name in ('one.DNG', 'two.DNG'):
                self.write(left / name); self.write(right / name)
            result, summary = self.run_scan(left, right, root / 'report', '--max-new', '1')
            self.assertEqual(result.returncode, 1)
            self.assertFalse(summary['complete'])
            self.assertFalse(summary['left_subsumed_by_right'])


if __name__ == '__main__':
    unittest.main()
