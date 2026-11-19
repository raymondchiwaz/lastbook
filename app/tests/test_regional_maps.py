"""Offline regional maps: database lifecycle, search and the real tile API.

Run with .venv-low-vram/bin/python -m unittest discover -s app/tests -p test_regional_maps.py -v
No network calls or downloaded library required.
"""
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import server
from engine import regional_maps

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('build_overview_maps', ROOT / 'tools/build_overview_maps.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fixture(data, label='Harare'):
    p = regional_maps.path(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix('.new')
    with sqlite3.connect(temp) as db:
        db.executescript('''
          CREATE TABLE feat(id INTEGER PRIMARY KEY,layer TEXT,cls TEXT,name TEXT,minz INTEGER,geom BLOB);
          CREATE VIRTUAL TABLE feat_idx USING rtree(id,minx,maxx,miny,maxy,minz,maxz);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE place(name TEXT,kind TEXT,detail TEXT,lat REAL,lon REAL,rank INTEGER);
        ''')
        db.execute('INSERT INTO feat VALUES (1,?,?,?,?,?)', ('place', 'city', label, 3, builder.pack([[(31.05, -17.83)]])))
        db.execute('INSERT INTO feat_idx VALUES (1,31.05,31.05,-17.83,-17.83,3,30)')
        db.execute('INSERT INTO place VALUES (?,?,?,?,?,?)', (label, 'City', 'Zimbabwe', -17.83, 31.05, 3))
        db.executemany('INSERT INTO meta VALUES (?,?)', {'title': 'Test overview', 'source': 'Natural Earth',
            'license': 'Public domain', 'built': 'test', 'bounds': '[-180,-60,180,85]',
            'home': '{"lat":-19,"lon":29.15,"z":6}', 'features': '1'}.items())
    temp.replace(p)


def detail_fixture(data, slug='zimbabwe', lon=31.05, lat=-17.83):
    p = regional_maps.path(data).parent / (slug + '.sqlite')
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as db:
        db.executescript('''
          CREATE TABLE feat(id INTEGER PRIMARY KEY,layer TEXT,cls TEXT,name TEXT,minz INTEGER,geom BLOB);
          CREATE VIRTUAL TABLE feat_idx USING rtree(id,minx,maxx,miny,maxy,minz,maxz);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE place(name TEXT,kind TEXT,detail TEXT,lat REAL,lon REAL,rank INTEGER);
          CREATE TABLE poi(id INTEGER PRIMARY KEY,cls TEXT,name TEXT,lon REAL,lat REAL,detail TEXT);
          CREATE INDEX poi_cls ON poi(cls,lon,lat);
        ''')
        db.execute('INSERT INTO feat VALUES (1,?,?,?,?,?)', ('road', 'residential', 'Test Road', 13, builder.pack([[(lon, lat), (lon+.001, lat+.001)]])))
        db.execute('INSERT INTO feat_idx VALUES (1,?,?,?,?,13,30)', (lon, lon+.001, lat, lat+.001))
        detail = json.dumps({'access': 'private', 'drinking_water': 'no', 'warning': 'Unverified mapped record'})
        db.executemany('INSERT INTO poi VALUES (?,?,?,?,?,?)',
                       [(1, 'hospital', 'Test Hospital', lon, lat, detail),
                        (2, 'water_well', 'Test Well', lon+.001, lat, detail),
                        (3, 'reservoir', 'Test Reservoir', lon, lat+.001, detail)])
        db.execute('INSERT INTO place VALUES (?,?,?,?,?,?)', ('Test Hospital', 'Hospital', slug, lat, lon, 10))
        db.executemany('INSERT INTO meta VALUES (?,?)', {'title': slug.title(), 'source': 'OpenStreetMap',
            'license': 'ODbL 1.0', 'built': 'test', 'bounds': json.dumps([lon-.01, lat-.01, lon+.01, lat+.01]),
            'data_as_of': '2026-11-16', 'layers': '["hospital","water_well","reservoir"]',
            'features': '1', 'pois': '3'}.items())


class RegionalMapsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)

    def tearDown(self):
        old = getattr(regional_maps._local, 'map', None)
        if old:
            old[1].close()
            regional_maps._local.map = None
        old = getattr(regional_maps._local, 'details', None)
        if old:
            for db, _ in old[1]:
                db.close()
            regional_maps._local.details = None
        regional_maps._cached_info.cache_clear()
        server._map_tile.cache_clear()
        server._pins.cache_clear()
        self.temp.cleanup()

    def test_optional_database(self):
        self.assertIsNone(regional_maps.database(self.data))
        self.assertIsNone(regional_maps.info(self.data))
        self.assertEqual(regional_maps.places(self.data, 'Harare'), [])
        self.assertEqual(regional_maps.rows(self.data, 0, -30, 40, 0, 8), [])

    def test_geometry_roundtrip_southern_and_eastern_hemispheres(self):
        parts = [[(31.05, -17.83), (77.2, 28.61)], [(121.47, 31.2)]]
        self.assertEqual(server.unpack(builder.pack(parts)), parts)

    def test_search_and_bounds(self):
        fixture(self.data)
        self.assertEqual(regional_maps.places(self.data, 'hara')[0]['name'], 'Harare')
        self.assertEqual(regional_maps.places(self.data, '%'), [])
        self.assertEqual(regional_maps.places(self.data, '_'), [])
        self.assertEqual(regional_maps.places(self.data, ''), [])
        self.assertEqual(regional_maps.places(self.data, "'; DROP TABLE place;--"), [])
        self.assertEqual(len(regional_maps.rows(self.data, 30, -18, 32, -17, 8)), 1)
        self.assertEqual(regional_maps.rows(self.data, -120, 30, -110, 40, 8), [])
        self.assertEqual(regional_maps.rows(self.data, 30, -18, 32, -17, 2), [])
        self.assertEqual(regional_maps.info(self.data)['home']['lat'], -19)

    def test_database_replacement_refreshes_connections(self):
        fixture(self.data)
        before = regional_maps.version(self.data)
        self.assertEqual(regional_maps.places(self.data, 'Harare')[0]['name'], 'Harare')
        fixture(self.data, 'Bulawayo')
        self.assertNotEqual(before, regional_maps.version(self.data))
        self.assertEqual(regional_maps.places(self.data, 'Harare'), [])
        self.assertEqual(regional_maps.places(self.data, 'Bulawayo')[0]['name'], 'Bulawayo')

    def test_server_without_original_us_map(self):
        fixture(self.data)
        with patch.object(server, 'HERE', self.data), patch.object(server, 'MAPDB', self.data / 'absent'), \
             patch.object(server, 'PLACEDB', self.data / 'absent'), patch.object(server, 'mapdb', return_value=None), \
             patch.object(server, 'national_mapdb', return_value=None), patch.object(server, 'pack_info', return_value=None):
            z = 8
            x = int((31.05 + 180) / 360 * 2**z)
            y = int((1 - math.asinh(math.tan(math.radians(-17.83))) / math.pi) / 2 * 2**z)
            tile = json.loads(server.map_tile(z, x, y))
            self.assertEqual(tile['layers']['place'][0][1], 'Harare')
            self.assertTrue(server.places('Harare')[0]['onMap'])
            info = server.map_info()
            self.assertEqual(info['home']['lat'], -19)
            self.assertEqual(info['regional']['features'], 1)
            self.assertIn('not street navigation', info['regional']['limitations'])
            self.assertEqual(info['layers'], [])

    def test_detailed_maps_pins_members_nearest_search_and_install_refresh(self):
        fixture(self.data)
        before = regional_maps.version(self.data)
        self.assertEqual(regional_maps.detail_databases(self.data), [])
        detail_fixture(self.data)
        detail_fixture(self.data, 'sri-lanka', lon=79.86, lat=6.93)
        self.assertNotEqual(before, regional_maps.version(self.data))
        self.assertEqual(len(regional_maps.poi_databases(self.data, 30, -18, 32, -17)), 1)
        self.assertEqual(len(regional_maps.poi_databases(self.data, 79, 6, 81, 8)), 1)
        with patch.object(server, 'HERE', self.data), patch.object(server, 'MAPDB', self.data / 'absent'), \
             patch.object(server, 'PLACEDB', self.data / 'absent'), patch.object(server, 'mapdb', return_value=None), \
             patch.object(server, 'national_mapdb', return_value=None), patch.object(server, 'pack_info', return_value=None):
            info = server.map_info()
            self.assertEqual(info['layers'], ['hospital', 'reservoir', 'water_well'])
            self.assertEqual(len(info['regional']['regions']), 2)
            z = 15
            x = int((31.05+180)/360*2**z)
            y = int((1-math.asinh(math.tan(math.radians(-17.83)))/math.pi)/2*2**z)
            tile = json.loads(server.map_tile(z, x, y))
            self.assertEqual(tile['layers']['road'][0][1], 'Test Road')
            kinds = ['hospital', 'water_well', 'reservoir']
            pins = server.pins(31, -17.9, 31.1, -17.8, 17, kinds)
            self.assertEqual(pins['sites'], 3)
            self.assertTrue(all('OpenStreetMap' in p['source'] for p in pins['points']))
            members = server.members(31, -17.9, 31.1, -17.8, kinds, -17.83, 31.05)
            self.assertEqual(members['total'], 3)
            self.assertEqual(members['items'][0]['detail']['access'], 'private')
            nearest = server.near(-17.83, 31.05, kinds)
            self.assertEqual(len(nearest), 3)
            self.assertEqual(nearest[0]['record']['drinking_water'], 'no')
            self.assertEqual(server.places('Test Hospital')[0]['source'], 'OpenStreetMap')
            self.assertEqual(server.pins(-5, 45, 0, 50, 10, kinds)['sites'], 0)

    def test_country_map_without_any_overview(self):
        detail_fixture(self.data)
        self.assertIsNone(regional_maps.database(self.data))
        self.assertEqual(regional_maps.info(self.data)['regions'][0]['title'], 'Zimbabwe')
        self.assertEqual(len(regional_maps.rows(self.data, 31, -18, 32, -17, 15)), 1)
        self.assertEqual(len(regional_maps.places(self.data, 'Test Hospital')), 1)


if __name__ == '__main__':
    unittest.main()
