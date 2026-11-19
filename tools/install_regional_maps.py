#!/usr/bin/env python3
"""Explicit, restartable country downloads and offline OSM map builds.

Build environment: osmium==4.1.1 and curl. Nothing is downloaded by the map UI.
  python tools/install_regional_maps.py --data /path/to/LastbookData \
      --continents africa asia --snapshot 261116
Or select a few countries: --countries zimbabwe botswana sri-lanka

Installs each country atomically, so completed maps work while others download.
Keeps source PBFs, checks Geofabrik's transfer MD5, records SHA-256 in map metadata,
and uses disk-backed node indexing. A progress JSON and per-country build logs
remain in derived/regional-maps/. Interrupted build databases are never discarded.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PRIORITY = ['zimbabwe', 'botswana', 'zambia', 'mozambique', 'south-africa', 'namibia',
            'swaziland', 'malawi', 'sri-lanka', 'china', 'india', 'kenya', 'tanzania', 'nepal']


def catalog():
    with urllib.request.urlopen('https://download.geofabrik.de/index-v1.json', timeout=60) as response:
        raw = response.read(8 * 1024 * 1024)
    out = {}
    for feature in json.loads(raw)['features']:
        p = feature['properties']
        slug, parent = p.get('id', ''), p.get('parent')
        if parent not in ('africa', 'asia') or not re.fullmatch('[a-z0-9-]+', slug):
            continue
        if slug in ('sea', 'south-africa-and-lesotho', 'east-timor'):
            continue  # overlapping extracts; East Timor is in Indonesia
        url = f'https://download.geofabrik.de/{parent}/{slug}-latest.osm.pbf'
        if p.get('urls', {}).get('pbf') == url:
            out[slug] = {'slug': slug, 'title': str(p['name']), 'continent': parent, 'url': url}
    return out


def digest(path, algorithm):
    h = hashlib.new(algorithm)
    with path.open('rb') as f:
        while b := f.read(4 * 1024 * 1024):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', required=True, type=Path)
    ap.add_argument('--continents', nargs='+', choices=['africa', 'asia'])
    ap.add_argument('--countries', nargs='+')
    ap.add_argument('--skip-countries', nargs='*', default=[])
    ap.add_argument('--snapshot', required=True, help='Geofabrik immutable YYMMDD snapshot (not latest)')
    args = ap.parse_args()
    if not re.fullmatch(r'\d{6}', args.snapshot):
        ap.error('snapshot must be YYMMDD')
    if not args.continents and not args.countries:
        ap.error('Select continents or countries explicitly')
    folder = args.data / 'derived/regional-maps'
    folder.mkdir(parents=True, exist_ok=True)
    sources = folder / 'sources'
    sources.mkdir(exist_ok=True)
    lock = (folder / '.install.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    all_regions = catalog()
    if any(s not in all_regions for s in args.countries or []):
        ap.error('Unknown country; use the Geofabrik country slug')
    selected = [r for r in all_regions.values() if r['slug'] not in args.skip_countries and
                (r['continent'] in (args.continents or []) or r['slug'] in (args.countries or []))]
    selected.sort(key=lambda r: (PRIORITY.index(r['slug']) if r['slug'] in PRIORITY else len(PRIORITY), r['title']))
    state = {'state': 'starting', 'current': None, 'total': len(selected), 'ready': [], 'failed': [],
             'snapshot': args.snapshot, 'scope': 'Geofabrik Africa/Asia country extracts; Russia and Turkey not included'}

    def save():
        state['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        tmp = folder / 'install-status.json.tmp'
        tmp.write_text(json.dumps(state, indent=2) + '\n')
        tmp.replace(folder / 'install-status.json')
        print(json.dumps(state), flush=True)

    for region in selected:
        slug = region['slug']
        target = folder / (slug + '.sqlite')
        if target.exists():
            # Runtime does the same read-only check; don't rebuild or overwrite an installed country.
            import sqlite3
            with sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError(f'Installed map needs inspection: {target}')
            state['ready'].append(region['title'])
            save()
            continue
        state.update(current=region['title'], state='downloading')
        save()
        url = region['url'].replace('-latest.', '-' + args.snapshot + '.')
        pbf = sources / url.rsplit('/', 1)[1]
        partial = pbf.with_suffix(pbf.suffix + '.partial')
        try:
            if shutil.disk_usage(folder).free < 30 * 1024**3:
                raise RuntimeError('Fewer than 30 GiB free; refusing to fill the disk')
            with urllib.request.urlopen(url + '.md5', timeout=60) as response:
                md5 = response.read(512).decode().split()[0].lower()
            if not re.fullmatch('[0-9a-f]{32}', md5):
                raise ValueError('Invalid source checksum response')
            log = folder / (slug + '.install.log')
            with log.open('a') as output:
                if not pbf.exists():
                    subprocess.run(['curl', '-fL', '--continue-at', '-', '--retry', '4', '--retry-all-errors',
                                    '--retry-delay', '10', '--connect-timeout', '30', '--max-time', '7200',
                                    '--output', str(partial), url], stdout=output, stderr=output, check=True)
                    if digest(partial, 'md5') != md5:
                        raise ValueError('Transfer checksum mismatch; partial retained for inspection')
                    partial.replace(pbf)
                elif digest(pbf, 'md5') != md5:
                    raise ValueError('Cached source checksum mismatch; source retained for inspection')
                state['state'] = 'importing'
                save()
                subprocess.run([sys.executable, '-u', str(ROOT / 'tools/build_regional_osm.py'), '--input', str(pbf),
                                '--data', str(args.data), '--slug', slug, '--title', region['title'],
                                '--source-url', url, '--disk-index'], stdout=output, stderr=output, check=True, timeout=10800)
            state['ready'].append(region['title'])
        except Exception as error:
            state['failed'].append({'country': region['title'], 'error': str(error)[:500]})
        save()
    state.update(state='done' if not state['failed'] else 'partial', current=None)
    save()
    return bool(state['failed'])


if __name__ == '__main__':
    sys.exit(main())
