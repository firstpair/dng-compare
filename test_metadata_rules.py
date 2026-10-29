import unittest

import metadata_rules


class MetadataRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = metadata_rules.load()

    def test_copy_groups_are_dropped(self):
        self.assertEqual(self.rules.normalize('EXIF:ExifIFD:Copy1:LensModel'), 'EXIF:ExifIFD:LensModel')
        self.assertEqual(self.rules.normalize('XMP:XMP-xmp:Rating'), 'XMP:XMP-xmp:Rating')

    def test_compared_and_category(self):
        self.assertFalse(self.rules.compared('SourceFile'))
        self.assertFalse(self.rules.compared('Composite:GPSPosition'))
        self.assertFalse(self.rules.compared('File:System:FileAccessDate'))
        self.assertTrue(self.rules.compared('File:System:FileSize'))
        self.assertEqual(self.rules.category('File:System:FileSize'), 'filesystem')
        self.assertEqual(self.rules.category('XMP:XMP-crs:Exposure2012'), 'embedded')

    def test_rendering_fails_closed(self):
        for tag in ('XMP:XMP-xmp:Rating', 'XMP:XMP-dc:Subject', 'IPTC:Keywords',
                    'EXIF:GPS:Copy1:GPSLatitude', 'File:System:FileSize'):
            self.assertTrue(self.rules.non_rendering(tag), tag)
        for tag in ('XMP:XMP-crs:Exposure2012', 'XMP:XMP-tiff:Orientation',
                    'EXIF:IFD0:Orientation', 'EXIF:ExifIFD:LensModel',
                    'MakerNotes:Panasonic:SerialNumber', 'XMP:XMP-unknown:Field'):
            self.assertFalse(self.rules.non_rendering(tag), tag)

    def test_layout_and_mirrors(self):
        self.assertTrue(self.rules.layout('EXIF:IFD0:StripOffsets'))
        self.assertTrue(self.rules.layout('EXIF:SubIFD2:Copy1:JpgFromRawStart'))
        self.assertFalse(self.rules.layout('XMP:XMP-x:StripOffsets'))
        self.assertFalse(self.rules.layout('EXIF:IFD0:StripByteCounts'))
        self.assertEqual(self.rules.mirror_sources('XMP:XMP-aux:Lens'), ['LensModel'])
        self.assertEqual(self.rules.mirror_sources('XMP:XMP-crs:Exposure2012'), [])
        self.assertTrue(self.rules.non_rendering('XMP:XMP-aux:SerialNumber'))


if __name__ == '__main__':
    unittest.main()
