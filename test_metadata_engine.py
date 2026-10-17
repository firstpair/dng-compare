"""Optional exiftool-rs engine plumbing; synthetic fixtures only, native workers skipped when absent."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np
import tifffile

import portable_metadata as metadata

PROJECT = pathlib.Path(__file__).resolve().parent
RUST = PROJECT / 'rust-scanner/target/release/metadata-extract'
OCAML = PROJECT / 'metadata-extract-exiftool-rs-oxcaml'


def engine_available(worker):
    if not worker.exists(): return False
    return subprocess.run([str(worker), '--engine-version', 'exiftool-rs'], capture_output=True).returncode == 0


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


class EngineSelection(unittest.TestCase):
    def test_unknown_engine_is_rejected(self):
        with mock.patch.dict(os.environ, {'DNG_METADATA_ENGINE': 'perl'}):
            with self.assertRaises(ValueError): metadata.extract(['/nonexistent'])

    def test_rs_engine_requires_native_worker(self):
        with mock.patch.dict(os.environ, {'DNG_METADATA_ENGINE': 'exiftool-rs'}, clear=False):
            os.environ.pop('DNG_METADATA_BACKEND', None)
            with self.assertRaisesRegex(ValueError, 'requires a native'): metadata.extract(['/nonexistent'])

    def test_engine_flag_forwarded_only_when_explicit(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append(command); return subprocess.CompletedProcess(command, 0, b'[]', b'')
        with mock.patch.dict(os.environ, {'DNG_METADATA_BACKEND': '/bin/worker', 'DNG_METADATA_WORKERS': '2'}, clear=False):
            os.environ.pop('DNG_METADATA_ENGINE', None)
            with mock.patch('subprocess.run', fake_run): metadata.extract(['/a'])
            self.assertEqual(calls[-1], ['/bin/worker', '2'])
            with mock.patch.dict(os.environ, {'DNG_METADATA_ENGINE': 'exiftool-rs'}), mock.patch('subprocess.run', fake_run):
                metadata.extract(['/a'])
            self.assertEqual(calls[-1], ['/bin/worker', '2', '--engine', 'exiftool-rs'])

    def test_c_abi_header_copies_match(self):
        rust = (PROJECT / 'rust-scanner/exiftool-ffi/dng_exiftool_rs.h').read_bytes()
        ocaml = (PROJECT / 'ocaml-exiftool-rs/lib/dng_exiftool_rs.h').read_bytes()
        self.assertEqual(rust, ocaml, 'keep the OCaml project header identical to the Rust crate header')


class NativeWorkers(unittest.TestCase):
    def fixture(self, root):
        good = root / 'good.DNG'
        tifffile.imwrite(good, np.arange(256, dtype=np.uint16).reshape(16, 16), metadata=None,
                         extratags=[(50706, 'B', 4, (1, 4, 0, 0), False)])
        return [str(good), str(root / 'missing.DNG'), str(good)]

    def check(self, worker, payload):
        with tempfile.TemporaryDirectory() as td:
            paths = self.fixture(pathlib.Path(td))
            proc = subprocess.run([str(worker), '2', '--engine', 'exiftool-rs'], input=payload(paths), capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))
            rows = json.loads(proc.stdout, object_pairs_hook=unique_object)
            self.assertEqual([r['SourceFile'] for r in rows], paths)
            for row in rows: self.assertIn('exiftool-rs', row['ExifTool:DngCompareEngine'])
            self.assertEqual(rows[0]['EXIF:IFD0:ImageWidth'], 16)
            # Perl ExifTool -n prints multi-count EXIF values space-joined, not as arrays.
            self.assertEqual(rows[0]['EXIF:IFD0:DNGVersion'], '1 4 0 0')
            self.assertEqual(rows[0]['Composite:ImageSize'], '16 16')
            self.assertEqual(rows[0]['File:FileType'], 'DNG')
            self.assertEqual(rows[0], rows[2])
            self.assertIn('I/O error', rows[1]['ExifTool:Error'])
            self.assertFalse(any(k.rsplit(':', 1)[-1] in metadata.EXCLUDE for r in rows for k in r))
            with mock.patch.dict(os.environ, {'DNG_METADATA_BACKEND': str(worker), 'DNG_METADATA_WORKERS': '2', 'DNG_METADATA_ENGINE': 'exiftool-rs'}):
                tags, _ = metadata.extract(paths[:2])
            self.assertEqual(set(tags), set(paths[:2]))
            self.assertEqual([e for e in [dict(side='apo', error=v) for k, v in tags[paths[1]].items() if k.endswith(':Error')]][0]['side'], 'apo')
            version = subprocess.run([str(worker), '--engine-version', 'exiftool-rs'], capture_output=True, text=True)
            self.assertEqual(version.stdout.strip(), rows[0]['ExifTool:DngCompareEngine'])
            self.assertNotEqual(subprocess.run([str(worker), '2', '--engine', 'bogus'], input=b'', capture_output=True).returncode, 0)

    def test_rust_worker(self):
        if not engine_available(RUST): self.skipTest('metadata-extract not built with the exiftool-rs feature')
        self.check(RUST, lambda paths: json.dumps(paths).encode())

    def test_ocaml_worker(self):
        if not engine_available(OCAML): self.skipTest('metadata-extract-exiftool-rs-oxcaml not built')
        self.check(OCAML, lambda paths: ('\0'.join(paths) + '\0').encode())


if __name__ == '__main__':
    unittest.main()
