import tempfile
import unittest
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import tifffile

from compare_dng import compare_pixels, compare_metadata, metadata, match, normalize_tags, Summary


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pixels = np.arange(256, dtype=np.uint16).reshape(16, 16)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, pixels=None, description='first', **kwargs):
        path = self.root / name
        tifffile.imwrite(path, self.pixels if pixels is None else pixels,
                         description=description, metadata=None,
                         extratags=[(50706, 'B', 4, (1, 4, 0, 0), False)], **kwargs)
        return str(path)

    def test_metadata_and_offsets_do_not_change_pixels(self):
        a = self.write('a.dng')
        b = self.write('b.dng', description='much longer metadata ' * 100)
        result = compare_pixels((a, b))
        self.assertEqual(result['raw']['status'], 'equal')
        self.assertEqual(result['raw']['images'][0]['method'], 'encoded_blocks')
        meta = metadata([a, b])
        diff = compare_metadata(meta[a]['tags'], meta[b]['tags'])
        self.assertTrue(any(c['tag'].endswith('ImageDescription') for c in diff['embedded']))

    def test_changed_pixel(self):
        a = self.write('a.dng')
        changed = self.pixels.copy()
        changed[9, 4] += 1
        b = self.write('b.dng', changed)
        self.assertEqual(compare_pixels((a, b))['raw']['status'], 'different')

    def test_different_compression_same_pixels(self):
        a = self.write('a.dng')
        b = self.write('b.dng', compression='deflate', rowsperstrip=4)
        result = compare_pixels((a, b))
        self.assertEqual(result['raw']['status'], 'equal')
        self.assertEqual(result['raw']['images'][0]['method'], 'decoded_samples')

    def test_different_endian_same_pixels(self):
        a = self.write('a.dng', byteorder='<')
        b = self.write('b.dng', byteorder='>')
        self.assertEqual(compare_pixels((a, b))['raw']['status'], 'equal')

    def test_truncated_image_is_unverified(self):
        a = self.write('a.dng')
        b = self.write('b.dng')
        with open(b, 'r+b') as f:
            f.truncate(Path(b).stat().st_size - 100)
        self.assertEqual(compare_pixels((a, b))['raw']['status'], 'unverified')

    def test_duplicate_names_compare_each_source(self):
        pairs, ambiguous = match({'x.dng': ['a/x.dng', 'b/x.dng']}, {'x.dng': ['c/x.dng']})
        self.assertEqual(pairs, [('a/x.dng', 'c/x.dng'), ('b/x.dng', 'c/x.dng')])
        self.assertEqual(len(ambiguous), 1)

    def test_metadata_values_and_presence(self):
        diff = compare_metadata({'XMP:Rating': 1, 'IFD0:StripOffsets': 20},
                                {'XMP:Rating': 2, 'IFD0:StripOffsets': 40, 'XMP:Label': ''})
        self.assertEqual(len(diff['embedded']), 2)
        self.assertEqual(len(diff['layout']), 1)

    def test_duplicate_relative_paths_are_matched(self):
        pairs, ambiguous = match({'x.dng': ['/a/one/x.dng', '/a/two/x.dng']},
                                 {'x.dng': ['/b/two/x.dng', '/b/one/x.dng']}, ['/a', '/b'])
        self.assertEqual(pairs[0], ('/a/one/x.dng', '/b/one/x.dng'))
        self.assertEqual(pairs[2], ('/a/two/x.dng', '/b/two/x.dng'))
        self.assertEqual(len(pairs), 4)
        self.assertEqual(len(ambiguous), 1)

    def test_exiftool_copy_numbers_do_not_create_false_changes(self):
        a = normalize_tags({'ExifIFD:Copy1:WhiteBalance': 0, 'XMP:WhiteBalance': 'Auto'})
        b = normalize_tags({'ExifIFD:WhiteBalance': 0, 'XMP:Copy2:WhiteBalance': 'Auto'})
        self.assertEqual(a, b)
        d = normalize_tags({'IFD0:Tag': 1, 'IFD0:Copy1:Tag': 2})
        self.assertEqual(d['IFD0:Tag']['duplicate_values'], [1, 2])

    def test_cli_resume_and_invalidation(self):
        for side in ('left', 'right'):
            (self.root / side).mkdir()
            self.write(f'{side}/same.dng')
        self.write('right/extra.dng')
        output = self.root / 'report'
        command = [sys.executable, str(Path(__file__).with_name('compare_dng.py')),
                   str(self.root / 'left'), str(self.root / 'right'), '--output', str(output)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['cached_pairs_reused'], 1)
        changed = self.pixels.copy()
        changed[0, 0] = 42
        self.write('right/same.dng', pixels=changed)
        third = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(third.returncode, 1, third.stderr)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual(summary['cached_pairs_reused'], 0)
        self.assertEqual(summary['counts']['raw_different'], 1)

    def test_subsumption_accepts_one_equal_candidate(self):
        summary = Summary()
        for status in ('different', 'equal'):
            summary.add({'left': '/left/a.dng', 'raw': {'status': status},
                         'auxiliary': {'status': status}, 'metadata_status': 'ok',
                         'differences': {'embedded': []}})
        self.assertTrue(summary.coverage['/left/a.dng']['raw'])
        path = self.root / 'summary.json'
        summary.write(path, {'left_files': 2}, True, 0)
        self.assertFalse(json.loads(path.read_text())['left_subsumed_by_right']['raw'])

    def test_same_size_binary_metadata_is_compared(self):
        paths = []
        for name, data in [('a', b'firstvalue'), ('b', b'othervalue')]:
            path = self.root / (name + '.dng')
            tifffile.imwrite(path, self.pixels, metadata=None,
                extratags=[(50706, 'B', 4, (1, 4, 0, 0), False),
                           (50740, 'B', len(data), data, False)])
            paths.append(str(path))
        meta = metadata(paths)
        diff = compare_metadata(meta[paths[0]]['tags'], meta[paths[1]]['tags'])
        self.assertTrue(any('DNGPrivateData' in c['tag'] for c in diff['embedded']), diff)


if __name__ == '__main__':
    unittest.main()
