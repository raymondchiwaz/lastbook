#!/usr/bin/env python3
"""Build an offline country map from an OSM PBF (build dependency: osmium==4.1.1).

No tile scraping or runtime service. Downloads are explicit, external to this tool.
  python tools/build_regional_osm.py --input zimbabwe.osm.pbf --data /path/to/LastbookData \
    --slug zimbabwe --title Zimbabwe --source-url https://download.geofabrik.de/africa/zimbabwe-261116.osm.pbf

Keeps roads/tracks/paths, rail, waterways, water and vegetation polygons, settlements
and survival-relevant tagged POIs (including mapped areas). Ordinary buildings are
omitted to keep country maps compact. Retains source dates, hashes, OSM ids, access,
water quality/seasonality and opening/contact tags where supplied. Does not verify
water safety, current service availability, legal access, hazards or routing.
The resulting database is OpenStreetMap-derived data under ODbL 1.0.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time

from build_map import ROAD_Z, PLACE_Z, LANDUSE_KEEP, area_z, pack

AMENITIES = {'hospital', 'clinic', 'doctors', 'pharmacy', 'dentist', 'veterinary',
             'fire_station', 'police', 'drinking_water', 'shelter', 'community_centre',
             'townhall', 'school', 'library', 'marketplace', 'toilets', 'fuel', 'bus_station',
             'ferry_terminal', 'embassy', 'social_facility', 'water_point'}
SHOPS = {'supermarket', 'convenience', 'hardware', 'doityourself', 'car_repair',
         'bicycle', 'greengrocer', 'bakery', 'butcher', 'agrarian', 'chemist'}
ALIASES = {'townhall': 'town_hall', 'toilets': 'toilet', 'bicycle': 'bicycle_shop'}
DETAIL_TAGS = ('access', 'operator', 'opening_hours', 'phone', 'contact:phone', 'website',
               'emergency', 'healthcare', 'healthcare:speciality', 'drinking_water',
               'seasonal', 'intermittent', 'pump', 'shelter_type', 'surface', 'ford',
               'bridge', 'toll', 'fee', 'capacity', 'addr:street', 'addr:housenumber', 'addr:city')


def poi_class(tags):
    # A decommissioned facility must not look like a currently mapped service.
    if any(tags.get(k) in ('yes', 'true', '1') for k in ('disused', 'abandoned', 'demolished')):
        return None
    amenity = tags.get('amenity')
    if amenity in AMENITIES:
        return ALIASES.get(amenity, amenity)
    if tags.get('healthcare') in ('hospital', 'clinic', 'doctor', 'pharmacy'):
        return {'doctor': 'doctors'}.get(tags['healthcare'], tags['healthcare'])
    shop = tags.get('shop')
    if shop in SHOPS:
        return ALIASES.get(shop, shop)
    if tags.get('natural') == 'spring':
        return 'spring'
    if tags.get('man_made') in ('water_well', 'water_tap', 'water_works', 'water_tower'):
        return tags['man_made']
    if tags.get('tourism') in ('camp_site', 'wilderness_hut', 'alpine_hut'):
        return tags['tourism']
    if tags.get('barrier') == 'border_control':
        return 'border_control'
    if tags.get('aeroway') == 'aerodrome':
        return 'aerodrome'
    if tags.get('railway') == 'station':
        return 'railway_station'
    if tags.get('ford') in ('yes', 'stepping_stones') or tags.get('highway') == 'ford':
        return 'ford'
    if tags.get('waterway') == 'dam':
        return 'osm_dam'
    if tags.get('highway') and tags.get('bridge') in ('yes', 'viaduct', 'movable'):
        return 'bridge'
    if tags.get('landuse') == 'reservoir' or tags.get('water') == 'reservoir':
        return 'reservoir'
    if tags.get('natural') == 'water':
        return 'lake'
    return None


def polygon_style(tags):
    if tags.get('natural') == 'water' or tags.get('landuse') in ('reservoir', 'basin'):
        return 'water', tags.get('water') or tags.get('landuse') or 'water'
    if tags.get('natural') == 'wetland':
        return 'wet', 'wetland'
    cls = LANDUSE_KEEP.get(tags.get('landuse'))
    if not cls and tags.get('natural') in ('wood', 'scrub', 'grassland', 'heath'):
        cls = 'green'
    if not cls and tags.get('leisure') in ('park', 'nature_reserve'):
        cls = 'park'
    return ('land', cls) if cls else None


def build(source, data, slug, title, url, disk_index=False):
    import osmium
    folder = data / 'derived' / 'regional-maps'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (slug + '.sqlite')
    staging = target.with_suffix('.building.sqlite')
    # Use exclusive creation: never overwrite somebody else's in-progress build.
    with staging.open('xb'):
        pass
    db = sqlite3.connect(staging)
    header = osmium.io.Reader(str(source))
    try:
        date = header.header().get('osmosis_replication_timestamp') or 'Unknown (see source file)'
    finally:
        header.close()
    factory = osmium.geom.GeoJSONFactory()
    counts = {'features': 0, 'pois': 0, 'places': 0, 'invalid_geometry': 0}
    bounds = [180., 90., -180., -90.]
    try:
        db.executescript('''PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL;
          CREATE TABLE feat(id INTEGER PRIMARY KEY,layer TEXT,cls TEXT,name TEXT,minz INTEGER,geom BLOB);
          CREATE VIRTUAL TABLE feat_idx USING rtree(id,minx,maxx,miny,maxy,minz,maxz);
          CREATE TABLE poi(id INTEGER PRIMARY KEY,cls TEXT,name TEXT,lon REAL,lat REAL,detail TEXT);
          CREATE INDEX poi_cls ON poi(cls,lon,lat);
          CREATE TABLE place(id INTEGER PRIMARY KEY,name TEXT,kind TEXT,detail TEXT,lat REAL,lon REAL,rank INTEGER);
          CREATE INDEX place_name ON place(name COLLATE NOCASE);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        ''')

        def add_place(name, kind, lon, lat, rank=10):
            if name:
                db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank) VALUES (?,?,?,?,?,?)',
                           (name, kind, title + ' · OpenStreetMap', lat, lon, rank))
                counts['places'] += 1

        def add_feature(layer, cls, name, parts, zoom):
            if not parts or any(not math.isfinite(v) for p in parts for v in p):
                return
            xs = [v for p in parts for v in p[::2]]
            ys = [v for p in parts for v in p[1::2]]
            if not xs or min(xs) < -180 or max(xs) > 180 or min(ys) < -90 or max(ys) > 90:
                return
            bbox = (min(xs), max(xs), min(ys), max(ys))
            bounds[:] = [min(bounds[0], bbox[0]), min(bounds[1], bbox[2]), max(bounds[2], bbox[1]), max(bounds[3], bbox[3])]
            if zoom is None:
                zoom = area_z(*bbox, 8)
            counts['features'] += 1
            fid = counts['features']
            db.execute('INSERT INTO feat VALUES (?,?,?,?,?,?)', (fid, layer, cls, name, zoom, pack(parts)))
            db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (fid, *bbox, zoom, 30))
            if fid % 50000 == 0:
                db.commit()
                print(title, counts, flush=True)

        def add_poi(tags, obj_id, lon, lat):
            cls = poi_class(tags)
            if not cls:
                return
            name = tags.get('name') or tags.get('name:en') or tags.get('ref') or ''
            detail = {k: str(tags[k])[:500] for k in DETAIL_TAGS if tags.get(k)}
            detail.update(osm_id=obj_id, data_as_of=date,
                          warning='Mapped record, not a verified operating service or safe water source. Check access locally.')
            counts['pois'] += 1
            db.execute('INSERT INTO poi VALUES (?,?,?,?,?,?)',
                       (counts['pois'], cls, name, lon, lat, json.dumps(detail, ensure_ascii=False)))
            add_place(name, cls.replace('_', ' ').title(), lon, lat)

        class Handler(osmium.SimpleHandler):
            def node(self, n):
                if not len(n.tags) or not n.location.valid():
                    return
                tags = dict(n.tags)
                lon, lat = n.location.lon, n.location.lat
                add_poi(tags, f'node/{n.id}', lon, lat)
                cls = tags.get('place')
                if cls:
                    name = tags.get('name') or tags.get('name:en')
                    if name:
                        z = PLACE_Z.get(cls, 14)
                        add_feature('place', cls, name, [[lon, lat]], z)
                        add_place(name, cls.title(), lon, lat, z)

            def way(self, w):
                tags = dict(w.tags)
                road = tags.get('highway')
                waterway = tags.get('waterway')
                rail = tags.get('railway')
                cls = poi_class(tags)
                if not (road or waterway or rail or cls):
                    return
                try:
                    parts = [[v for n in w.nodes for v in (n.lon, n.lat)]]
                except osmium.InvalidLocationError:
                    counts['invalid_geometry'] += 1
                    return
                if not parts[0]:
                    return
                name = tags.get('name') or tags.get('ref') or ''
                if road and road not in ('proposed', 'construction', 'abandoned'):
                    add_feature('road', road, name, parts, ROAD_Z.get(road, 15))
                elif waterway in ('river', 'stream', 'canal', 'drain', 'ditch'):
                    add_feature('waterway', waterway, name, parts, 9 if waterway == 'river' else 13)
                elif rail in ('rail', 'light_rail', 'tram', 'narrow_gauge'):
                    add_feature('rail', rail, name, parts, 11)
                # Closed service ways are emitted by area() to avoid duplicates.
                if cls and not w.is_closed():
                    add_poi(tags, f'way/{w.id}', parts[0][0], parts[0][1])

            def area(self, a):
                tags = dict(a.tags)
                style = polygon_style(tags)
                cls = poi_class(tags)
                if not style and not cls:
                    return
                try:
                    polygons = json.loads(factory.create_multipolygon(a))['coordinates']
                except (RuntimeError, ValueError):
                    counts['invalid_geometry'] += 1
                    return
                # Keep every ring, including islands/holes, in the existing even-odd geometry format.
                parts = [[v for p in ring for v in p[:2]] for poly in polygons for ring in poly]
                if not parts:
                    return
                name = tags.get('name') or tags.get('name:en') or ''
                if style:
                    add_feature(*style, name, parts, None)
                # A polygon's bounding-box centre can lie outside it. Use a mapped
                # boundary coordinate instead; this is NOT a verified entrance.
                lon, lat = parts[0][:2]
                if cls:
                    add_poi(tags, f'{"way" if a.from_way() else "relation"}/{a.orig_id()}', lon, lat)
                elif style and style[0] == 'water':
                    add_place(name, 'Water body (untested)', lon, lat)

        with tempfile.TemporaryDirectory(prefix='.node-cache-', dir=folder) as cache:
            index = 'sparse_file_array,' + str(Path(cache) / 'nodes') if disk_index else 'flex_mem'
            Handler().apply_file(str(source), locations=True, idx=index)
        if not counts['features']:
            raise ValueError('No map features imported; existing installation retained')
        h = hashlib.sha256()
        with source.open('rb') as f:
            while b := f.read(4 * 1024 * 1024):
                h.update(b)
        classes = [r[0] for r in db.execute('SELECT DISTINCT cls FROM poi ORDER BY cls')]
        meta = {'title': title, 'detail': 'street', 'source': 'OpenStreetMap contributors via Geofabrik',
                'license': 'ODbL 1.0 — https://opendatacommons.org/licenses/odbl/1-0/',
                'source_url': url, 'source_sha256': h.hexdigest(), 'data_as_of': date,
                'built': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bounds': json.dumps(bounds),
                'layers': json.dumps(classes), **{k: str(v) for k, v in counts.items()}}
        db.executemany('INSERT INTO meta VALUES (?,?)', meta.items())
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
    finally:
        db.close()
    staging.replace(target)
    target.with_suffix('.sources.json').write_text(json.dumps(meta, indent=2) + '\n')
    print('REGIONAL_MAP_READY', target, counts, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--data', required=True, type=Path)
    ap.add_argument('--slug', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--source-url', required=True)
    ap.add_argument('--disk-index', action='store_true', help='Keep node positions on disk instead of RAM for large countries')
    args = ap.parse_args()
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', args.slug) or args.slug == 'overview':
        ap.error('slug must be a country name in lowercase, with optional hyphens (not overview)')
    if sys.byteorder != 'little':
        ap.error('Map geometry currently requires little-endian hardware')
    build(args.input, args.data, args.slug, args.title, args.source_url, args.disk_index)


if __name__ == '__main__':
    main()
