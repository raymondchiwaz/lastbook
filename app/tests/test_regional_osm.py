"""OSM importer tests. Run with the maps build venv (osmium==4.1.1), no network."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from build_regional_osm import build, poi_class

XML = '''<osm version="0.6" generator="Lastbook-test">
<node id="1" lat="-17.8300" lon="31.0500"><tag k="place" v="village"/><tag k="name" v="Test Village"/></node>
<node id="2" lat="-17.8300" lon="31.0510"><tag k="amenity" v="drinking_water"/><tag k="name" v="Test Tap"/><tag k="drinking_water" v="no"/><tag k="access" v="private"/></node>
<node id="3" lat="-17.8310" lon="31.0510"><tag k="man_made" v="water_well"/><tag k="seasonal" v="yes"/></node>
<node id="4" lat="-17.8310" lon="31.0500"><tag k="amenity" v="clinic"/><tag k="disused" v="yes"/><tag k="name" v="Closed Clinic"/></node>
<node id="5" lat="-17.8320" lon="31.0500"/>
<node id="6" lat="-17.8320" lon="31.0510"/>
<node id="7" lat="-17.8330" lon="31.0510"/>
<node id="8" lat="-17.8330" lon="31.0500"/>
<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/><tag k="name" v="Test Road"/></way>
<way id="11"><nd ref="2"/><nd ref="3"/><tag k="highway" v="track"/><tag k="ford" v="yes"/></way>
<way id="12"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/><tag k="amenity" v="hospital"/><tag k="building" v="hospital"/><tag k="name" v="Test Hospital"/><tag k="phone" v="test-number"/></way>
<way id="13"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="natural" v="water"/><tag k="name" v="Test Lake"/></way>
</osm>'''


class ClassifierTest(unittest.TestCase):
    def test_categories_and_lifecycle(self):
        for tag, value, expected in [('shop', 'supermarket', 'supermarket'), ('amenity', 'marketplace', 'marketplace'),
                                     ('man_made', 'water_well', 'water_well'), ('barrier', 'border_control', 'border_control'),
                                     ('tourism', 'wilderness_hut', 'wilderness_hut'), ('healthcare', 'doctor', 'doctors')]:
            self.assertEqual(poi_class({tag: value}), expected)
        self.assertIsNone(poi_class({'amenity': 'hospital', 'abandoned': 'yes'}))
        self.assertIsNone(poi_class({'disused:amenity': 'hospital'}))
        self.assertIsNone(poi_class({'amenity': 'bench'}))


@unittest.skipUnless(importlib.util.find_spec('osmium'), 'build-only osmium dependency not installed')
class ImporterTest(unittest.TestCase):
    def test_full_import_keeps_warning_tags_and_area_services(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'test.osm'
            source.write_text(XML)
            build(source, root, 'test-country', 'Test Country', 'https://example.invalid/test.osm', disk_index=True)
            target = root / 'derived/regional-maps/test-country.sqlite'
            with sqlite3.connect(target) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute("SELECT COUNT(*) FROM feat WHERE layer='road'").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM poi WHERE cls='hospital'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM poi WHERE name='Closed Clinic'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM poi WHERE cls='lake'").fetchone()[0], 1)
                detail = json.loads(db.execute("SELECT detail FROM poi WHERE name='Test Tap'").fetchone()[0])
                self.assertEqual(detail['drinking_water'], 'no')
                self.assertEqual(detail['access'], 'private')
                self.assertIn('not a verified', detail['warning'])
                self.assertEqual(detail['osm_id'], 'node/2')
                meta = dict(db.execute('SELECT key,value FROM meta'))
                self.assertIn('ODbL', meta['license'])
                self.assertEqual(len(meta['source_sha256']), 64)
                self.assertIn('hospital', json.loads(meta['layers']))
            old = target.read_bytes()
            (root / 'derived/regional-maps/test-country.building.sqlite').write_bytes(b'other build')
            with self.assertRaises(FileExistsError):
                build(source, root, 'test-country', 'Test Country', 'test')
            self.assertEqual(target.read_bytes(), old)


if __name__ == '__main__':
    unittest.main()
