"""Optional offline overview maps, separate from the original US/state map set.

The importer writes the same feat/feat_idx geometry format as tools/build_map.py.
No downloads happen here. Reopening on file replacement makes installed maps usable
without restarting the app, and keeps state-map/library updates independent.
"""
import json
import hashlib
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

from .paths import ro_uri

_local = threading.local()


def path(data):
    return Path(data) / 'derived' / 'regional-maps' / 'overview.sqlite'


def version(data):
    stamps = []
    for p in sorted(path(data).parent.glob('*.sqlite')):
        if p.name.endswith('.building.sqlite'):
            continue
        try:
            s = p.stat()
            stamps.append((p.name, s.st_ino, s.st_mtime_ns, s.st_size))
        except OSError:
            continue
    return hashlib.sha256(repr(stamps).encode()).hexdigest()[:20] if stamps else 'none'


def database(data):
    p = path(data)
    try:
        s = p.stat()
        stamp = (s.st_ino, s.st_mtime_ns, s.st_size)
    except OSError:
        stamp = 'none'
    key = (str(p), stamp)
    old = getattr(_local, 'map', None)
    if old and old[0] == key:
        return old[1]
    if old:
        old[1].close()
        _local.map = None
    if key[1] == 'none':
        return None
    db = sqlite3.connect(ro_uri(p), uri=True)
    db.execute('PRAGMA query_only=ON')
    _local.map = (key, db)
    return db


def rows(data, west, south, east, north, zoom):
    found = []
    dbs = ([database(data)] if database(data) is not None else []) + poi_databases(data, west, south, east, north)
    for db in dbs:
        found += db.execute('SELECT f.layer,f.cls,f.name,f.geom FROM feat_idx i JOIN feat f ON f.id=i.id '
                           'WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=? ORDER BY f.id',
                           (west, east, south, north, zoom)).fetchall()
    return found


def detail_databases(data):
    """Per-thread read-only country connections, refreshed after atomic installations."""
    key = (str(Path(data)), version(data))
    old = getattr(_local, 'details', None)
    if old and old[0] == key:
        return old[1]
    if old:
        for db, _ in old[1]:
            db.close()
    out = []
    for p in sorted(path(data).parent.glob('*.sqlite')):
        if p.name == 'overview.sqlite' or p.name.endswith('.building.sqlite'):
            continue
        db = sqlite3.connect(ro_uri(p), uri=True)
        db.execute('PRAGMA query_only=ON')
        meta = dict(db.execute('SELECT key,value FROM meta'))
        meta.update(bounds=json.loads(meta['bounds']), layers=json.loads(meta['layers']), bytes=p.stat().st_size)
        out.append((db, meta))
    _local.details = (key, out)
    return out


def poi_databases(data, west=-180, south=-90, east=180, north=90):
    return [db for db, m in detail_databases(data)
            if m['bounds'][2] >= west and m['bounds'][0] <= east and m['bounds'][3] >= south and m['bounds'][1] <= north]


@lru_cache(maxsize=8)
def _cached_info(data, stamp):
    return _info(data)


def info(data):
    return _cached_info(str(data), version(data))


def _info(data):
    db = database(data)
    regions = [dict(m) for _, m in detail_databases(data)]
    if db is None and not regions:
        return None
    meta = dict(db.execute('SELECT key,value FROM meta')) if db else {
        **regions[0], 'home': json.dumps({'lat': -19, 'lon': 29.15, 'z': 6}),
        'bounds': json.dumps(regions[0]['bounds'])}
    return {'title': meta['title'], 'source': meta['source'], 'license': meta['license'],
            'built': meta['built'], 'bounds': json.loads(meta['bounds']),
            'home': json.loads(meta['home']), 'detail': 'overview',
            'features': int(meta['features']), 'bytes': path(data).stat().st_size if db else 0,
            'regions': regions, 'layers': sorted({c for m in regions for c in m['layers']}),
            'limitations': 'Generalised overview, not street navigation. No verified emergency services, '
                            'water safety, live hazards, terrain or routing. Source layers have different ages.'}


def places(data, query, limit=20):
    db = database(data)
    query = query.strip()
    if not query:
        return []
    # Escape LIKE wildcards; a user searching for '%' must not return the world.
    term = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    out = []
    for connection, source in [(d, 'OpenStreetMap') for d in poi_databases(data)] + ([(db, 'Natural Earth')] if db else []):
        found = connection.execute("SELECT name,kind,detail,lat,lon FROM place WHERE name LIKE ? ESCAPE '\\' "
                                   'ORDER BY (lower(name)=lower(?)) DESC, rank, name LIMIT ?',
                                   (term + '%', query, min(100, max(1, limit)))).fetchall()
        for n, k, d, la, lo in found:
            if any(n.casefold() == p['name'].casefold() and abs(la-p['lat']) < .03 and abs(lo-p['lon']) < .03 for p in out):
                continue
            out.append({'name': n, 'kind': k, 'detail': d, 'lat': la, 'lon': lo, 'source': source, 'onMap': True})
    out.sort(key=lambda p: p['name'].casefold() != query.casefold())
    return out[:limit]
