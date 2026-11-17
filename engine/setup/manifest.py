"""What can be installed: manifest-v1.json, bundled with the app at build time (packaging/release_manifest.py), or fetched once
from the download site when setup is opened. A fetched one is used only if it parses, says version 1 and every address in it is
allowed (download.url_allowed)."""
import json
import os
import re
import sys
import threading
from pathlib import Path

from . import download

REMOTE = 'https://downloads.lastbook.org/manifest-v1.json'
SHA = re.compile(r'^[0-9a-f]{64}$')
LIBRARY_HOSTS = frozenset({'downloads.lastbook.org'})   # packs and state maps come from Lastbook's own download site only
MODEL_HOSTS = frozenset({'huggingface.co'})             # the model from Hugging Face only (it redirects to its own CDN hosts)


def _from(url, hosts, allowed):
    """An allowed https address, and in the product (allowed is None) on one of the hosts for this kind of file."""
    if not download.url_allowed(url, allowed):
        return False
    if allowed is not None:
        return True
    import urllib.parse
    return (urllib.parse.urlsplit(url).hostname or '').lower() in hosts
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$')


def bundled_paths():
    if getattr(sys, 'frozen', False):
        base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
        return [base / 'setup' / 'manifest-v1.json']
    root = Path(__file__).resolve().parents[2]
    return [root / 'setup' / 'manifest-v1.json', root / 'derived' / 'release' / 'setup' / 'manifest-v1.json']


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate(m, allowed=None):
    """The manifest, cleaned to the fields setup uses, or None if anything in it is wrong."""
    if not isinstance(m, dict) or m.get('version') != 1:
        return None
    packs, models = [], []
    for p in m.get('packs') or []:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or not NAME.match(p['id']):
            return None
        revision = p.get('revision', '')
        if not isinstance(revision, str) or (revision and not NAME.fullmatch(revision)):
            return None
        files = []
        for f in p.get('files') or []:
            if not (isinstance(f, dict) and isinstance(f.get('name'), str) and NAME.match(f['name']) and _int(f.get('size'))
                    and isinstance(f.get('sha256'), str) and SHA.match(f['sha256']) and _from(f.get('url'), LIBRARY_HOSTS, allowed)):
                return None
            files.append({'name': f['name'], 'url': f['url'], 'size': f['size'], 'sha256': f['sha256'], 'kind': f.get('kind') or 'zip'})
        available = bool(p.get('available')) and bool(files)
        if available and not _int(p.get('installed_bytes')):
            return None
        packs.append({'id': p['id'], 'title': str(p.get('title') or p['id']), 'summary': str(p.get('summary') or ''),
                      'records': p.get('records') if _int(p.get('records')) else None, 'revision': revision,
                      'installed_bytes': p.get('installed_bytes') if _int(p.get('installed_bytes')) else None,
                      'download_bytes': sum(f['size'] for f in files), 'available': available, 'files': files,
                      'map': p['map'] if isinstance(p.get('map'), str) and NAME.match(p['map']) and p['map'] != 'base' else None})
    collections = {}
    for key in ('maps', 'address_indexes'):
        entries = []
        for x in m.get(key) or []:
            if not isinstance(x, dict) or not isinstance(x.get('id'), str) or not NAME.match(x['id']):
                return None
            if key == 'address_indexes' and not re.fullmatch(r'[A-Z]{2}', x['id']):
                return None
            files = []
            for f in x.get('files') or []:
                if not (isinstance(f, dict) and isinstance(f.get('name'), str) and NAME.match(f['name']) and _int(f.get('size'))
                        and isinstance(f.get('sha256'), str) and SHA.match(f['sha256']) and _from(f.get('url'), LIBRARY_HOSTS, allowed)):
                    return None
                files.append({'name': f['name'], 'url': f['url'], 'size': f['size'], 'sha256': f['sha256'], 'kind': f.get('kind') or 'zip'})
            available = bool(x.get('available')) and bool(files) and all(f['size'] for f in files)
            if available and not _int(x.get('installed_bytes')):
                return None
            entries.append({'id': x['id'], 'title': str(x.get('title') or x['id']), 'kind': str(x.get('kind') or 'state'),
                         'includes': str(x.get('includes') or ''),
                         'installed_bytes': x.get('installed_bytes') if _int(x.get('installed_bytes')) else None,
                         'download_bytes': sum(f['size'] for f in files), 'available': available, 'files': files})
        collections[key] = entries
    for x in m.get('models') or []:
        if not (isinstance(x, dict) and isinstance(x.get('id'), str) and NAME.match(x['id']) and isinstance(x.get('file'), str)
                and NAME.match(x['file']) and _int(x.get('size')) and isinstance(x.get('sha256'), str) and SHA.match(x['sha256'])
                and _from(x.get('url'), MODEL_HOSTS, allowed) and _int(x.get('memory_bytes'))):
            return None
        models.append({'id': x['id'], 'title': str(x.get('title') or x['id']), 'file': x['file'], 'url': x['url'], 'size': x['size'],
                       'sha256': x['sha256'], 'memory_bytes': x['memory_bytes'], 'available': bool(x.get('available', True))})
    return {'version': 1, 'published': str(m.get('published') or ''), 'packs': packs, 'models': models, **collections}


def load_bundled(paths=None):
    for p in paths or bundled_paths():
        try:
            m = validate(json.loads(Path(p).read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
        if m:
            return m
    return {'version': 1, 'published': '', 'packs': [], 'models': [], 'maps': []}


def merge(bundled, fetched):
    """What the app was built with stays (audit E1): for an id the bundled manifest pins (it has files, or the model's file),
    its addresses, sizes and SHA-256 are kept and the fetched copy may only change whether it is available. A fetched copy may
    add ids, and give the files of an id the app was built without (a pack published later). One bad publish never takes an
    option away."""
    out = dict(fetched)
    for key in ('packs', 'models', 'maps', 'address_indexes'):
        built = {x['id']: x for x in bundled.get(key, [])}
        merged = []
        for x in fetched.get(key, []):
            b = built.pop(x['id'], None)
            pinned = b is not None and (b.get('files') or b.get('sha256'))
            merged.append({**b, 'available': bool(x.get('available'))} if pinned else x)
        out[key] = merged + list(built.values())
    return out


class Manifest:
    """The bundled manifest at once; a fetched one replaces it when it arrives and is newer or the same date."""

    def __init__(self, bundled=None, remote=REMOTE, allowed=None, context=None):
        self.current = bundled or load_bundled()
        self.source = 'bundled'
        self.remote, self.allowed, self.context = remote, allowed, context
        self.fetching = None
        self._apply_profile()

    def _apply_profile(self):
        # A profile owns its model pins. A site manifest must never replace the small model with Gemma.
        if os.environ.get('LASTBOOK_AI_PROFILE') != 'low-vram' or self.allowed is not None:
            return
        import llm_local
        repo, revision = llm_local.SOURCE.split(', revision ')
        entry = {'id': llm_local.MODEL_ID, 'title': llm_local.MODEL_TITLE, 'file': llm_local.FILE,
                 'url': f'https://huggingface.co/{repo}/resolve/{revision}/{llm_local.FILE}?download=true',
                 'size': llm_local.SIZE, 'sha256': llm_local.SHA256,
                 'memory_bytes': 2 * 1024**3, 'available': True}  # conservative estimate; measured once downloaded
        self.current = dict(self.current, models=[entry])

    def fetch_once(self):
        if self.fetching or not self.remote:
            return
        self.fetching = threading.Thread(target=self._fetch, daemon=True, name='setup-manifest')
        self.fetching.start()

    def _fetch(self):
        data = download.fetch_small(self.remote, allowed=self.allowed, context=self.context)
        if not data:
            return
        try:
            m = validate(json.loads(data.decode('utf-8')), self.allowed)
        except ValueError:
            m = None
        if m:      # merge() keeps every pinned entry, so a fetched copy can only add or mark available, whatever its date
            self.current, self.source = merge(self.current, m), 'fetched'
            self._apply_profile()

    def pack(self, pid):
        return next((p for p in self.current['packs'] if p['id'] == pid), None)

    def map(self, mid):
        return next((x for x in self.current.get('maps', []) if x['id'] == mid), None)

    def address_index(self, state):
        return next((x for x in self.current.get('address_indexes', []) if x['id'] == state), None)

    def model(self, mid=None):
        ms = [m for m in self.current['models'] if m['available']]
        return next((m for m in ms if mid in (None, m['id'])), None)

    def public(self):
        strip = lambda f: {k: v for k, v in f.items() if k != 'url'}
        return {'source': self.source, 'published': self.current['published'],
                'packs': [{**p, 'files': [strip(f) for f in p['files']]} for p in self.current['packs']],
                'models': [strip(m) for m in self.current['models']],
                'maps': [{**x, 'files': [strip(f) for f in x['files']]} for x in self.current.get('maps', [])],
                'address_indexes': [{**x, 'files': [strip(f) for f in x['files']]} for x in self.current.get('address_indexes', [])]}
