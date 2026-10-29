"""Fixture-only checks for develop-settings capture across editors."""
import contextlib
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import tifffile

import develop_settings as ds

PROJECT = Path(__file__).resolve().parent
RUST = PROJECT / 'rust-scanner' / 'target' / 'release' / 'develop-settings'

LRCAT_SETTINGS = '''s = {
\tAutoTone = true,
\tExposure2012 = 0.4,
\tShadows2012 = 58,
\tToneCurvePV2012 = {
\t\t0,
\t\t0,
\t\t255,
\t\t255,
\t},
\tLook = {
\t\tName = "Adobe Color",
\t\tParameters = { Version = "18.7" },
\t},
\tRawFileName = "Caf\\195\\169 \\"quoted\\".DNG",
\tWhiteBalance = "As Shot",
}
'''

COS = '''<?xml version="1.0"?>
<IMG>
\t<E K="Camera_Model" V="LEICA M10-R" />
\t<VAR Engine="1300">
\t\t<E K="UUID" V="27534708" />
\t\t<DL>
\t\t\t<E K="Exposure" V="0" />
\t\t\t<E K="WhiteBalance" V="2.2;1;2.26" />
\t\t</DL>
\t\t<AL>
\t\t\t<E K="Exposure" V="2.33291363716125" />
\t\t\t<E K="ShadowRecovery" V="69" />
\t\t</AL>
\t</VAR>
</IMG>
'''

XMP = '''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
 crs:Exposure2012="+0.34" crs:Shadows2012="+56" crs:AutoToneDigest="66192840B295D81C39ED6F4E74CD19F6"/>
</rdf:RDF></x:xmpmeta>'''


def make_jpeg(path, xmp):
    """A minimal JPEG holding only an XMP APP1 segment: enough for metadata readers."""
    payload = b'http://ns.adobe.com/xap/1.0/\x00' + xmp.encode()
    path.write_bytes(b'\xff\xd8\xff\xe1' + (len(payload) + 2).to_bytes(2, 'big') + payload + b'\xff\xda\x00\x02\xff\xd9')


def make_catalog(path, folder):
    db = sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE AgLibraryRootFolder(id_local INTEGER PRIMARY KEY, absolutePath TEXT);
    CREATE TABLE AgLibraryFolder(id_local INTEGER PRIMARY KEY, rootFolder INTEGER, pathFromRoot TEXT);
    CREATE TABLE AgLibraryFile(id_local INTEGER PRIMARY KEY, folder INTEGER, baseName TEXT, extension TEXT);
    CREATE TABLE Adobe_images(id_local INTEGER PRIMARY KEY, rootFile INTEGER, copyName TEXT);
    CREATE TABLE Adobe_imageDevelopSettings(id_local INTEGER PRIMARY KEY, image INTEGER, text TEXT, digest TEXT);
    CREATE TABLE Adobe_libraryImageDevelopHistoryStep(id_local INTEGER PRIMARY KEY, image INTEGER, name TEXT, dateCreated REAL);
    ''')
    parent = os.path.realpath(folder.parent).rstrip('/') + '/'
    db.execute('INSERT INTO AgLibraryRootFolder VALUES (1,?)', (parent,))
    db.execute('INSERT INTO AgLibraryFolder VALUES (1,1,?)', (folder.name + '/',))
    db.execute("INSERT INTO AgLibraryFile VALUES (1,1,'A','DNG'),(2,1,'B','DNG')")
    db.execute("INSERT INTO Adobe_images VALUES (10,1,NULL),(11,1,'Copy 1'),(12,2,NULL)")
    db.execute('INSERT INTO Adobe_imageDevelopSettings VALUES (1,10,?,?)', (LRCAT_SETTINGS, 'd1'))
    db.execute("INSERT INTO Adobe_imageDevelopSettings VALUES (2,11,'s = { Exposure2012 = 1.5 }','d2')")
    db.execute("INSERT INTO Adobe_imageDevelopSettings VALUES (3,12,'s = { Exposure2012 = -999999 }','d3')")
    db.execute("INSERT INTO Adobe_libraryImageDevelopHistoryStep VALUES (1,10,'Import',1),(2,10,'Auto Settings',2)")
    db.commit()
    db.close()


class DevelopSettings(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dng-develop-settings-')
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'photos' / 'album'
        self.folder.mkdir(parents=True)
        self.archive = self.root / 'archive'

    def tearDown(self):
        self.temporary.cleanup()

    def test_typed_values(self):
        self.assertEqual(ds.typed('+0.34'), 0.34)
        self.assertEqual(ds.typed('+56'), 56)
        self.assertEqual(ds.typed('-2'), -2)
        self.assertEqual(ds.typed('True'), True)
        self.assertEqual(ds.typed('As Shot'), 'As Shot')
        self.assertEqual(ds.typed('1;1;1'), '1;1;1')
        self.assertEqual(ds.typed('66192840B295D81C39ED6F4E74CD19F6'), '66192840B295D81C39ED6F4E74CD19F6')
        self.assertEqual(ds.typed('1234567890123456789'), '1234567890123456789')
        self.assertEqual(ds.typed(['-50', '0, 0']), [-50, '0, 0'])

    def test_lua_settings_parse(self):
        value = ds.parse_lua_assignment(LRCAT_SETTINGS)
        self.assertIs(value['AutoTone'], True)
        self.assertEqual(value['Exposure2012'], 0.4)
        self.assertEqual(value['ToneCurvePV2012'], [0, 0, 255, 255])
        self.assertEqual(value['Look'], {'Name': 'Adobe Color', 'Parameters': {'Version': '18.7'}})
        self.assertEqual(value['RawFileName'], 'Caf\xe9 "quoted".DNG')
        self.assertEqual(ds.parse_lua_assignment('s = { ["a b"] = 1, [2] = "x", 3 }'), {'a b': 1, '2': 'x', '1': 3})
        with self.assertRaises(ValueError):
            ds.parse_lua_assignment('s = { a = os.exit() }')

    def test_three_editors_into_one_archive(self):
        catalog = self.root / 'test.lrcat'
        make_catalog(catalog, self.folder)
        before = catalog.read_bytes()
        records, pending = ds.capture_lrcat(self.folder, catalog)
        self.assertEqual(catalog.read_bytes(), before)
        self.assertEqual([r.photo for r in records], ['A.DNG', 'A.DNG [Copy 1]', 'B.DNG'])
        self.assertEqual(pending, ['B.DNG'])
        self.assertEqual(records[0].raw.history, ['Import', 'Auto Settings'])
        self.assertEqual(records[0].raw.text, LRCAT_SETTINGS)
        ds.write_capture(self.archive, 'lrc', 'lrcat', ds.CaptureSource(folder=str(self.folder), catalog=str(catalog)), records)

        settings = self.folder / 'CaptureOne' / 'Settings141'
        settings.mkdir(parents=True)
        (settings / 'A.DNG.cos').write_text(COS)
        c1 = ds.capture_one(settings)
        self.assertEqual(c1[0].photo, 'A.DNG')
        self.assertEqual(c1[0].settings['CaptureOne:AL:Exposure'], 2.33291363716125)
        self.assertEqual(c1[0].settings['CaptureOne:DL:WhiteBalance'], '2.2;1;2.26')
        self.assertEqual(c1[0].raw.cos_xml, COS)
        ds.write_capture(self.archive, 'c1', 'capture-one', ds.CaptureSource(folder=str(self.folder), settings_dir=str(settings)), c1)

        snapshot = self.root / 'lr.jsonl.gz'
        with gzip.open(snapshot, 'wt') as f:
            f.write(json.dumps({'file': str(self.folder / 'A.DNG'), 'size': 1, 'mtime_ns': 2,
                                'tags': {'XMP-crs:Exposure2012': '+0.34', 'XMP-crs:Shadows2012': '+58', 'IFD0:Make': 'X'}}) + '\n')
        lr = ds.capture_xmp(self.folder, snapshot)
        self.assertEqual(lr[0].settings, {'XMP-crs:Exposure2012': 0.34, 'XMP-crs:Shadows2012': 58})
        self.assertEqual(lr[0].raw.exiftool['IFD0:Make'], 'X')
        ds.write_capture(self.archive, 'lr', 'xmp', ds.CaptureSource(folder=str(self.folder), snapshot=str(snapshot)), lr)

        with self.assertRaises(FileExistsError):
            ds.write_capture(self.archive, 'lr', 'xmp', ds.CaptureSource(folder=str(self.folder)), lr)

        rows = {r.left_tag: r for r in ds.compare(self.archive, 'lr', 'lrc')}
        # Numbers compare by value: Lightroom's '+1.00' text and Classic's 1 are the same setting.
        self.assertEqual(ds.typed('+1.00'), 1.0)
        self.assertEqual(rows['XMP-crs:Shadows2012'].identical, 1)
        self.assertAlmostEqual(rows['XMP-crs:Exposure2012'].median_difference, 0.06)
        mapped = ds.compare(self.archive, 'lr', 'c1', [('XMP-crs:Exposure2012', 'CaptureOne:AL:Exposure')])
        self.assertEqual(mapped[0].photos, 1)

        db = sqlite3.connect(self.archive / 'settings' / 'settings.sqlite3')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT value_json FROM setting_values WHERE label='lrc' AND photo='A.DNG' AND tag='XMP-crs:ToneCurvePV2012'").fetchone()[0], '[0,0,255,255]')
        counts = db.execute('SELECT COUNT(*) FROM settings').fetchone()
        db.close()
        ds.rebuild_index(self.archive)
        db = sqlite3.connect(self.archive / 'settings' / 'settings.sqlite3')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM settings').fetchone(), counts)
        db.close()

        provenance = json.loads((self.archive / 'settings' / 'lrc.provenance.json').read_text())
        self.assertEqual(provenance['schema'], ds.SCHEMA_VERSION)
        self.assertEqual(provenance['photos'], 3)

    def test_cli_refuses_uncomputed_auto(self):
        catalog = self.root / 'test.lrcat'
        make_catalog(catalog, self.folder)
        run = lambda *extra: subprocess.run([sys.executable, str(PROJECT / 'develop_settings.py'), 'capture', str(self.archive),
                                             'lrc', 'lrcat', str(self.folder), '--catalog', str(catalog), *extra],
                                            capture_output=True, text=True)
        refused = run()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('uncomputed Auto', refused.stderr)
        self.assertFalse((self.archive / 'settings' / 'lrc.jsonl.gz').exists())
        accepted = run('--allow-pending')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('uncomputed', json.loads(accepted.stdout)['notes'][0])

    @unittest.skipUnless(shutil.which('exiftool'), 'ExifTool not installed')
    def test_reads_settings_embedded_in_dng(self):
        path = self.folder / 'A.DNG'
        tifffile.imwrite(path, np.zeros((4, 4), np.uint16), extratags=[(700, 1, None, XMP.encode(), True)])
        records = ds.capture_xmp(self.folder)
        self.assertEqual(records[0].photo, 'A.DNG')
        self.assertEqual(records[0].settings['XMP-crs:Exposure2012'], 0.34)
        self.assertEqual(records[0].settings['XMP-crs:AutoToneDigest'], '66192840B295D81C39ED6F4E74CD19F6')

    @unittest.skipUnless(shutil.which('exiftool'), 'ExifTool not installed')
    def test_reads_settings_embedded_in_jpeg_exports(self):
        make_jpeg(self.folder / 'A.jpg', XMP)
        tifffile.imwrite(self.folder / 'A.DNG', np.zeros((4, 4), np.uint16), extratags=[(700, 1, None, XMP.encode(), True)])
        self.assertEqual([r.photo for r in ds.capture_xmp(self.folder)], ['A.DNG'])
        records = ds.capture_xmp(self.folder, extensions=('jpg',))
        self.assertEqual([r.photo for r in records], ['A.jpg'])
        self.assertEqual(records[0].settings['XMP-crs:Exposure2012'], 0.34)

    @unittest.skipUnless(RUST.exists() and shutil.which('exiftool'), 'Rust develop-settings not built or ExifTool missing')
    def test_rust_and_python_write_the_same_archive(self):
        catalog = self.root / 'test.lrcat'
        make_catalog(catalog, self.folder)
        settings = self.folder / 'CaptureOne' / 'Settings141'
        settings.mkdir(parents=True)
        (settings / 'A.DNG.cos').write_text(COS)
        tifffile.imwrite(self.folder / 'A.DNG', np.zeros((4, 4), np.uint16), extratags=[(700, 1, None, XMP.encode(), True)])
        make_jpeg(self.folder / 'A.jpg', XMP)
        python = lambda *a: subprocess.run([sys.executable, str(PROJECT / 'develop_settings.py'), *map(str, a)], capture_output=True, text=True)
        rust = lambda *a: subprocess.run([str(RUST), *map(str, a)], capture_output=True, text=True)
        for name, run in (('py', python), ('rs', rust)):
            for app, extra in (('xmp', []), ('lrcat', ['--catalog', catalog, '--allow-pending']), ('capture-one', [])):
                done = run('capture', self.archive, '%s-%s' % (name, app), app, self.folder, *extra)
                self.assertEqual(done.returncode, 0, done.stderr)
            done = run('capture', self.archive, '%s-jpeg' % name, 'xmp', self.folder, '--extensions', 'jpg')
            self.assertEqual(done.returncode, 0, done.stderr)
        for app in ('xmp', 'lrcat', 'capture-one', 'jpeg'):
            py = ds.read_records(self.archive / 'settings' / ('py-%s.jsonl.gz' % app))
            rs = ds.read_records(self.archive / 'settings' / ('rs-%s.jsonl.gz' % app))
            self.assertEqual([r.photo for r in py], [r.photo for r in rs])
            self.assertEqual([r.settings for r in py], [r.settings for r in rs], app)
            self.assertEqual([r.source for r in py], [r.source for r in rs], app)
        # Each implementation reindexes and compares the other's captures identically.
        def values():
            with contextlib.closing(sqlite3.connect(self.archive / 'settings' / 'settings.sqlite3')) as db:
                return db.execute('SELECT label,photo,tag,value_json,number FROM setting_values ORDER BY 1,2,3').fetchall()
        before = values()
        for run in (rust, python):
            self.assertEqual(run('reindex', self.archive).returncode, 0)
            self.assertEqual(values(), before)
        outputs = [[json.loads(l) for l in run('compare', self.archive, 'py-xmp', 'rs-lrcat').stdout.splitlines()] for run in (python, rust)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(outputs[0])


if __name__ == '__main__':
    unittest.main()
