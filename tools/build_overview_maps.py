#!/usr/bin/env python3
"""Download small Natural Earth layers and install a worldwide Lastbook overview.

Includes Africa and Asia. No OSM tile scraping, API key, GIS service or runtime
network dependency. Install pyshp (only build-time dependency), then:
  python tools/build_overview_maps.py --data /path/to/LastbookData

Sources: https://www.naturalearthdata.com/ (public domain). 1:10m means
1:10,000,000 cartographic scale, NOT ten-metre accuracy. These are historical,
generalised layers, not emergency-service or turn-by-turn navigation data.
"""
import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request
import zipfile

LAYERS = [
    ('10m_cultural', 'ne_10m_admin_0_countries', 'border'),
    ('10m_cultural', 'ne_10m_populated_places', 'place'),
    ('10m_cultural', 'ne_10m_roads', 'road'),
    ('10m_physical', 'ne_10m_rivers_lake_centerlines', 'waterway'),
    ('10m_physical', 'ne_10m_lakes', 'water'),
]


def pack(parts):
    a = array('i', [len(parts)])
    for points in parts:
        a.append(len(points))
        x0 = y0 = 0
        for lon, lat in points:
            x, y = round(lon * 1e6), round(lat * 1e6)
            a.extend((x - x0, y - y0))
            x0, y0 = x, y
    return a.tobytes()


def download(group, name, cache):
    url = f'https://naturalearth.s3.amazonaws.com/{group}/{name}.zip'
    dst = cache / (name + '.zip')
    if not dst.exists() or not zipfile.is_zipfile(dst):
        tmp = dst.with_suffix('.zip.partial')
        print('Downloading', url, flush=True)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=90) as src, tmp.open('wb') as out:
                    while chunk := src.read(1024 * 1024):
                        out.write(chunk)
                with zipfile.ZipFile(tmp) as z:
                    if z.testzip():
                        raise ValueError('Archive CRC check failed')
                tmp.replace(dst)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
    with zipfile.ZipFile(dst) as z:
        if z.testzip():
            raise ValueError(f'Archive CRC check failed: {dst}')
    return dst, {'url': url, 'sha256': hashlib.sha256(dst.read_bytes()).hexdigest(), 'bytes': dst.stat().st_size}


def build(data, cache):
    import shapefile
    folder = data / 'derived' / 'regional-maps'
    folder.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    sources = []
    # Download first: a failed transfer must not touch the installed map.
    archives = []
    for group, name, layer in LAYERS:
        archive, source = download(group, name, cache)
        archives.append((archive, name, layer))
        sources.append(source)
    tmp = folder / 'overview.building.sqlite'
    if tmp.exists():
        raise FileExistsError(f'Previous/interrupted build at {tmp}; inspect it before retrying')
    db = sqlite3.connect(tmp)
    try:
        db.executescript('''PRAGMA journal_mode=DELETE;
          CREATE TABLE feat(id INTEGER PRIMARY KEY,layer TEXT,cls TEXT,name TEXT,minz INTEGER,geom BLOB);
          CREATE VIRTUAL TABLE feat_idx USING rtree(id,minx,maxx,miny,maxy,minz,maxz);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE place(id INTEGER PRIMARY KEY,name TEXT,kind TEXT,detail TEXT,lat REAL,lon REAL,rank INTEGER);
          CREATE INDEX place_name ON place(name COLLATE NOCASE);''')
        count = 0
        for archive, name, layer in archives:
            with zipfile.ZipFile(archive) as z:
                # Read only known members, without extracting third-party archive paths.
                with z.open(name + '.shp') as shp, z.open(name + '.dbf') as dbf:
                    reader = shapefile.Reader(shp=shp, dbf=dbf, encoding='utf-8', encodingErrors='replace')
                    for item in reader.iterShapeRecords():
                        props = {k.lower(): v for k, v in item.record.as_dict().items()}
                        title = props.get('name_en') or props.get('name') or props.get('name_long') or ''
                        title = str(title)
                        if title.lower() in ('null', '-99'):
                            title = ''
                        points = item.shape.points
                        if not points or any(not math.isfinite(v) for pt in points for v in pt[:2]):
                            continue
                        indices = list(item.shape.parts) + [len(points)]
                        if len(indices) == 1:
                            indices.insert(0, 0)
                        parts = [[(p[0], p[1]) for p in points[a:b]] for a, b in zip(indices, indices[1:]) if b > a]
                        cls, minz = layer, 5
                        if layer == 'road':
                            roadtype = str(props.get('type', '')).lower()
                            cls = 'motorway' if 'major highway' in roadtype else 'primary' if 'secondary' not in roadtype else 'secondary'
                            minz = 5 if cls == 'motorway' else 7 if cls == 'primary' else 9
                            title = title or str(props.get('roadname') or '')
                        elif layer == 'waterway':
                            cls, minz = 'river', max(5, min(10, int(props.get('min_zoom') or 5)))
                        elif layer == 'water':
                            cls, minz = 'lake', max(4, min(10, int(props.get('min_zoom') or 4)))
                        elif layer == 'border':
                            cls, minz = 'country', 3
                            lon, lat = props.get('label_x'), props.get('label_y')
                            if lon is not None and lat is not None:
                                db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank) VALUES (?,?,?,?,?,?)',
                                           (title, 'Country', 'Natural Earth overview', lat, lon, 0))
                        elif layer == 'place':
                            cls = 'city' if int(props.get('pop_max') or 0) >= 100000 else 'town'
                            minz = max(3, min(10, int(props.get('min_zoom') or 5)))
                            lon, lat = points[0][:2]
                            db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank) VALUES (?,?,?,?,?,?)',
                                       (title, cls.title(), str(props.get('adm0name') or 'Natural Earth overview'), lat, lon, minz))
                        xs = [p[0] for p in points]
                        ys = [p[1] for p in points]
                        count += 1
                        db.execute('INSERT INTO feat VALUES (?,?,?,?,?,?)', (count, layer, cls, title, minz, pack(parts)))
                        db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (count, min(xs), max(xs), min(ys), max(ys), minz, 30))
            db.commit()
            print(layer, 'imported; total features:', count, flush=True)
        meta = {'title': 'Africa, Asia & world overview', 'source': 'Natural Earth (1:10,000,000 scale)',
                'license': 'Public domain — naturalearthdata.com', 'built': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'bounds': json.dumps([-180, -60, 180, 85]),
                'home': json.dumps({'lat': -19.0, 'lon': 29.15, 'z': 6}),
                'features': str(count), 'sources': json.dumps(sources)}
        db.executemany('INSERT INTO meta VALUES (?,?)', meta.items())
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Map database integrity check failed')
    finally:
        db.close()
    tmp.replace(folder / 'overview.sqlite')
    (folder / 'SOURCES.json').write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')
    print('MAPS_READY', folder / 'overview.sqlite', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', required=True, type=Path)
    ap.add_argument('--cache', type=Path)
    args = ap.parse_args()
    if sys.byteorder != 'little':
        ap.error('The current map geometry format requires little-endian hardware')
    build(args.data, args.cache or args.data / 'derived' / 'regional-maps' / 'sources')


if __name__ == '__main__':
    main()
