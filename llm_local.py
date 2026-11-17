"""The optional local AI: Gemma 4, or a small Qwen model in the low-vram profile, run by lastbook-llm through pipes.

Nothing here opens a socket or reaches past this computer. The model is a file the person downloads from the website, outside the
app. Lastbook finds it inside the selected library or an explicitly configured model folder, checks the published file, asks llama.cpp
whether it fits this computer's memory, and starts it when someone asks a question. It stops after IDLE_STOP seconds unused,
when turned off, and when the app quits (the program leaves when its standard input closes).

assistant.py supplies OpenAI-shaped messages. This file renders the selected model's prompt format as segments (template
markup, where special tokens count, or plain text, where they never do: a document cannot start a turn of its own), and
turns the model's tool calls back into the shape assistant.py already handles.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from engine import paths

FILE = 'gemma-4-26B-A4B-it-UD-Q4_K_M.gguf'
SIZE = 16_947_541_728
SHA256 = 'f2c28b3dc4776931ac6f879e11f203dec637ea0f14267a86ec8f6165f63f293f'
NAME = 'Gemma 4 26B A4B (Unsloth 4-bit, UD-Q4_K_M)'
SOURCE = 'unsloth/gemma-4-26B-A4B-it-GGUF, revision c099eb48e663fd284577b04978a94ffccb261841'
CTX = 16384               # tokens the model holds: the system prompt, sources, the conversation and the answer
PROFILE = os.environ.get('LASTBOOK_AI_PROFILE', 'standard')
if PROFILE not in ('standard', 'low-vram'):
    raise ValueError('LASTBOOK_AI_PROFILE must be standard or low-vram')
LOW_VRAM = PROFILE == 'low-vram'
SMALL_MODEL = os.environ.get('LASTBOOK_AI_MODEL', 'qwen3.5-2b')
QWEN35 = LOW_VRAM and SMALL_MODEL.startswith('qwen3.5-')
MINICPM = LOW_VRAM and SMALL_MODEL == 'minicpm5-2b'
GPU_RESERVE_MB = 512
MODEL_ID = 'gemma-4-26b-a4b'
MODEL_TITLE = 'Gemma 4 26B A4B'
TOOL_MARKER = '<|tool_call>'
MAX_TOKENS = 1800
if LOW_VRAM:
    FILE = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
    SIZE = 1_117_320_736
    SHA256 = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
    NAME = 'Qwen2.5 1.5B Instruct (4-bit, low VRAM)'
    SOURCE = 'Qwen/Qwen2.5-1.5B-Instruct-GGUF, revision 91cad51170dc346986eccefdc2dd33a9da36ead9'
    MODEL_ID, MODEL_TITLE = 'qwen2.5-1.5b-q4-k-m', 'Qwen2.5 1.5B Instruct (low VRAM)'
    CTX, MAX_TOKENS, TOOL_MARKER = 4096, 768, '<tool_call>'
    if SMALL_MODEL == 'qwen3.5-2b':
        FILE, SIZE = 'Qwen3.5-2B-Q4_K_M.gguf', 1_280_835_840
        SHA256 = 'aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223'
        SOURCE = 'unsloth/Qwen3.5-2B-GGUF, revision f6d5376be1edb4d416d56da11e5397a961aca8ae'
        NAME = 'Qwen3.5 2B (4-bit, low VRAM)'
        MODEL_ID, MODEL_TITLE = 'qwen3.5-2b-q4-k-m', NAME
    elif SMALL_MODEL == 'qwen3.5-0.8b':
        FILE, SIZE = 'Qwen3.5-0.8B-Q8_0.gguf', 833_592_096
        SHA256 = '37ae482d336108d23516fa35e8e0c4126688d81018b87178a18d752a1357814f'
        SOURCE = 'ggml-org/Qwen3.5-0.8B-GGUF, revision 004fd086eccea9f7cf867aa07ba328773beef81d'
        NAME = 'Qwen3.5 0.8B (8-bit, extra VRAM headroom)'
        MODEL_ID, MODEL_TITLE = 'qwen3.5-0.8b-q8-0', NAME
    elif SMALL_MODEL == 'qwen3.5-2b-abliterated':
        FILE, SIZE = 'Huihui-Qwen3.5-2B-abliterated.Q4_K_M.gguf', 1_270_809_024
        SHA256 = 'aa25eea787afe56a097268f7ed3460cb623e1901d2e89cd2b654cabb42f80636'
        SOURCE = 'mradermacher/Huihui-Qwen3.5-2B-abliterated-GGUF, revision 7f5aee7d2dc4df18bd67eab85ea81e0bc2e98a2c'
        NAME = 'Huihui Qwen3.5 2B Abliterated (4-bit)'
        MODEL_ID, MODEL_TITLE = 'huihui-qwen3.5-2b-abliterated-q4-k-m', NAME
    elif SMALL_MODEL == 'minicpm5-2b':
        FILE, SIZE = 'MiniCPM5-2B-Q4_K_M.gguf', 1_561_318_368
        SHA256 = 'ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd'
        SOURCE = 'openbmb/MiniCPM5-2B-GGUF, revision 8ad02b786ab81e33d465e1c80562640c55ab2877'
        NAME = 'MiniCPM5 2B (4-bit, agent tools)'
        MODEL_ID, MODEL_TITLE = 'minicpm5-2b-q4-k-m', NAME
        CTX, MAX_TOKENS, GPU_RESERVE_MB, TOOL_MARKER = 3072, 512, 256, '<function'
    elif SMALL_MODEL != 'qwen2.5-1.5b':
        raise ValueError('Unknown LASTBOOK_AI_MODEL; see run-low-vram.sh --help for model choices')
RESERVE = 2.5 * 2**30     # memory left for the system and the rest of Lastbook beyond what llama.cpp measures for the model
IDLE_STOP = int(os.environ.get('LASTBOOK_AI_IDLE_S') or 15 * 60)   # seconds without a question before the model is stopped, to give its memory back
START_WAIT = 600          # longest a question waits for the model to load

STATE_FILE = paths.DATA / 'derived' / 'llm' / 'state.json'   # where the file was found, its check, the on/off setting


def runner_args(model, extra=()):
    """Use identical memory settings for setup's measurement and the actual process."""
    threads = max(1, min(8 if LOW_VRAM else 16, (os.cpu_count() or 4) // 2))
    args = ['--model', str(model), '--ctx', str(CTX), '--threads', str(threads)]
    if LOW_VRAM:
        args += ['--batch', '128', '--ubatch', '64', '--gpu-reserve-mb', str(GPU_RESERVE_MB)]
    if os.environ.get('LASTBOOK_AI_CPU_ONLY') == '1':
        args += ['--cpu-only']
    return args + list(extra)


def runner_path():
    exe = 'lastbook-llm.exe' if os.name == 'nt' else 'lastbook-llm'
    here = [paths.APP / 'llm' / exe]                         # in the packaged app
    if not paths.FROZEN:
        here.append(paths.APP / 'derived' / 'llm' / {'win32': 'bin-win', 'darwin': 'bin-mac'}.get(sys.platform, 'bin-linux') / exe)
    return next((p for p in here if p.is_file()), None)


def places():
    """The selected library and an explicitly configured model folder only."""
    out = [paths.DATA / 'models', paths.DATA]
    if os.environ.get('LASTBOOK_MODEL_DIR'):
        out.insert(0, Path(os.environ['LASTBOOK_MODEL_DIR']))
    seen, res = set(), []
    for p in out:
        k = str(p)
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


# ------------------------------------------------------------------------------------------------------ state on disk --
def _load():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(d):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, indent=1), encoding='utf-8')
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print('local AI: could not save its state:', e, flush=True)


def _stamp(p):
    st = p.stat()
    return [str(p), st.st_size, st.st_mtime_ns]


def mark_checked(path, sha256):
    """For setup: it has just checked this file's SHA-256 while downloading it, so the AI need not read it again.
    sha256 is the digest setup computed; it must be the published one, or nothing is recorded."""
    p = Path(path)
    if p.name != FILE or p.stat().st_size != SIZE or str(sha256).lower() != SHA256:
        raise ValueError('not the model file')
    LOCAL.saved['checked'] = _stamp(p)
    _save(LOCAL.saved)


# ----------------------------------------------------------------------------------------------------------- the model --
class Local:
    """One model process at a time. States:
      off        turned off in the app
      absent     no model file found
      wrong      a file of that name that is not the published one
      checking   reading the file to check it (pct)
      measuring  asking llama.cpp how it fits this computer
      too_big    it does not fit this computer's memory
      stopped    found, checked and fits; it starts when someone asks
      loading    starting (pct)
      ready      answering
      error      the program failed (message)
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.gen_lock = threading.Lock()       # one answer at a time: the model holds one conversation
        self.state, self.pct, self.message = 'absent', None, ''
        self.path, self.fit, self.ready_info = None, None, None
        self.proc, self.events, self.last_used = None, None, 0
        self.next_id = 0
        self.failed_loads = 0     # loads in a row that ended before the model was ready
        self.saved = _load()
        self.runner = runner_path()
        self._started = False

    # -- what the app shows -----------------------------------------------------------------------------------------------
    def status(self):
        with self.lock:
            fit = self.fit or {}
            return {'name': NAME, 'file': FILE, 'size': SIZE, 'state': self.state, 'pct': self.pct, 'message': self.message,
                    'idle_s': IDLE_STOP, 'profile': PROFILE,
                    'found_at': self.path and _folder_name(self.path), 'enabled': self.enabled(), 'runtime': bool(self.runner),
                    'needs_bytes': fit.get('needs'), 'ram_total': fit.get('ram_total'), 'placement': fit.get('placement'),
                    'load_s': (self.ready_info or {}).get('load_s'), 'places': [_folder_name(p) for p in places()[:6]]}

    def enabled(self):
        return self.saved.get('enabled', True)

    def available(self):
        """True when a question can be answered: running, or ready to start."""
        return self.state in ('ready', 'stopped', 'loading')

    # -- finding and checking the file ------------------------------------------------------------------------------------
    def start_background(self):
        if self._started:
            return
        self._started = True
        import atexit
        atexit.register(self.stop, 'Lastbook is closing')   # and if Lastbook dies instead, the closed pipe ends the program
        threading.Thread(target=self.discover, daemon=True, name='local-ai-find').start()
        threading.Thread(target=self._idle_watch, daemon=True, name='local-ai-idle').start()

    def discover(self):
        """Find the file, check it if it has not been checked as it is now, measure the fit. Safe to call again (Look again)."""
        with self.lock:
            if self.state in ('checking', 'measuring', 'loading', 'ready'):
                return
            if not self.enabled():
                self.state, self.message = 'off', ''
                return
            if not self.runner:
                self.state, self.message = 'error', 'This copy of Lastbook has no local AI runtime.'
                return
            self.state, self.pct, self.message = 'absent', None, ''
        found, wrong = None, None
        for folder in places():
            p = folder / FILE
            try:
                if not p.is_file():
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            if size == SIZE:
                found = p
                break
            wrong = wrong or (p, size)
        if not found:
            with self.lock:
                if wrong:
                    p, size = wrong
                    done = size < SIZE
                    self.state = 'wrong'
                    self.message = (f'The file in {_folder_name(p)} is {_gb(size)} of {_gb(SIZE)}. ' +
                                    ('The download may not have finished.' if done else 'It is not the file Lastbook expects.'))
                else:
                    self.state = 'absent'
            return
        stamp = _stamp(found)
        if self.saved.get('checked') != stamp:
            if not self._check(found):
                return
            self.saved['checked'] = stamp
            _save(self.saved)
        with self.lock:
            self.path = found
        self._measure()

    def _check(self, p):
        with self.lock:
            self.state, self.pct, self.message = 'checking', 0, ''
        h = hashlib.sha256()
        done = 0
        try:
            with open(p, 'rb') as f:
                while chunk := f.read(8 * 2**20):
                    h.update(chunk)
                    done += len(chunk)
                    self.pct = int(done * 100 / SIZE)
        except OSError as e:
            with self.lock:
                self.state, self.pct, self.message = 'wrong', None, f'Lastbook could not read the model file ({type(e).__name__}).'
            return False
        if h.hexdigest() != SHA256:
            with self.lock:
                self.state, self.pct = 'wrong', None
                self.message = f'The file in {_folder_name(p)} has the right size but different contents. Download it again.'
            return False
        return True

    def _measure(self):
        """llama.cpp's fitting decides where each part of the model goes; this then checks the whole fits this computer."""
        with self.lock:
            self.state, self.pct, self.message = 'measuring', None, ''
        try:
            out = subprocess.run(self._argv(['--measure']), stdin=subprocess.DEVNULL, capture_output=True, timeout=180, **_quiet())
            fit = next((json.loads(l) for l in out.stdout.decode('utf-8', 'replace').splitlines() if l.startswith('{"ev":"fit"')), None)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            fit, out = None, None
            print('local AI: measuring failed:', type(e).__name__, e, flush=True)
        with self.lock:
            if not fit:
                self.state, self.message = 'error', 'The local AI program could not read the model file.'
                if out is not None:
                    print('local AI: measure output:', out.stdout[-2000:], out.stderr[-3000:], flush=True)
                return
            self.fit = judge(fit)
            print('local AI: fit', json.dumps(self.fit), flush=True)
            if self.fit['fits']:
                self.state, self.message = 'stopped', ''
            else:
                self.state, self.message = 'too_big', self.fit['why']

    def _argv(self, extra=()):
        return [str(self.runner), *runner_args(self.path, extra)]

    # -- running ----------------------------------------------------------------------------------------------------------
    def ensure_running(self, on_wait=None, cancelled=lambda: False):
        """Starts the model if it is stopped and waits until it is ready. Returns None when ready, else a sentence why not."""
        with self.lock:
            if self.state == 'ready' and self.proc and self.proc.poll() is None:
                self.last_used = time.time()
                return None
            if self.state not in ('stopped', 'loading', 'error') or not self.path or not (self.fit or {}).get('fits'):
                return self.message or 'The local AI is not set up on this computer.'
            if self.state != 'loading':
                self._spawn()
        t0 = time.time()
        while time.time() - t0 < START_WAIT:
            with self.lock:
                if self.state == 'ready':
                    self.last_used = time.time()
                    return None
                if self.state != 'loading':
                    return self.message or 'The local AI did not start.'
            if cancelled():
                return 'stopped'
            if on_wait:
                on_wait(self.pct)
            time.sleep(0.25)
        self.stop('took too long to start')
        return 'The local AI took too long to start.'

    def _spawn(self):
        import queue
        self.state, self.pct, self.message = 'loading', 0, ''
        logs = paths.DATA / 'derived' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        err = open(logs / 'local-ai.log', 'ab')
        err.write(f'\n--- {time.strftime("%Y-%m-%d %H:%M:%S")} start\n'.encode())
        err.flush()
        self.proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, bufsize=0, **_quiet())
        _tie_to_this_process(self.proc)
        err.close()
        self.events = queue.Queue()
        self.t_spawn = time.time()
        threading.Thread(target=self._read, args=(self.proc, self.events), daemon=True, name='local-ai-read').start()

    def _read(self, proc, events):
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            kind = ev.get('ev')
            if kind == 'progress':
                with self.lock:
                    if self.proc is proc:
                        self.pct = ev.get('p')
            elif kind == 'ready':
                with self.lock:
                    if self.proc is proc:
                        self.state, self.pct = 'ready', None
                        self.failed_loads = 0
                        self.ready_info = dict(ev, load_s=round(time.time() - self.t_spawn, 1))
                        self.last_used = time.time()
                        print('local AI: ready', json.dumps(self.ready_info), flush=True)
            elif kind == 'fit':
                pass
            elif kind == 'error' and ev.get('id') is None:
                with self.lock:
                    if self.proc is proc and self.state == 'loading':
                        self.state, self.message = 'error', _plain_error(ev)
                events.put(ev)
            else:
                events.put(ev)
        code = proc.wait()
        events.put({'ev': 'exit', 'code': code})
        with self.lock:
            if self.proc is proc:
                self.proc = None
                print(f'local AI: the program ended with code {code}', flush=True)
                if self.state == 'ready':      # it was answering or waiting: the next question starts it again
                    self.state, self.message = 'stopped', ''
                elif self.state == 'loading':
                    self.failed_loads += 1
                    if self.failed_loads < 2:  # once may be chance (it was stopped from outside): the next question tries again
                        self.state, self.message = 'stopped', ''
                    else:                      # twice in a row: it would fail the same way again
                        self.state = 'error'
                        self.message = self.message or 'The local AI could not start. Look again after closing other programs.'

    def stop(self, why=''):
        with self.lock:
            proc, self.proc = self.proc, None
            if self.state in ('ready', 'loading', 'error') and self.path and (self.fit or {}).get('fits'):
                self.state, self.pct = 'stopped', None
                self.message = ''
        if proc and proc.poll() is None:
            print(f'local AI: stopping ({why or "asked"})', flush=True)
            try:
                proc.stdin.write(b'{"op":"quit"}\n')
                proc.stdin.close()
                proc.wait(10)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                proc.kill()

    def set_enabled(self, on):
        self.saved['enabled'] = bool(on)
        _save(self.saved)
        if on:
            with self.lock:
                if self.state == 'off':
                    self.state = 'absent'
            threading.Thread(target=self.discover, daemon=True).start()
        else:
            self.stop('turned off')
            with self.lock:
                self.state, self.message = 'off', ''

    def _idle_watch(self):
        while True:
            time.sleep(min(30, IDLE_STOP / 4))
            with self.lock:
                idle = self.state == 'ready' and not self.gen_lock.locked() and time.time() - self.last_used > IDLE_STOP
            if idle:
                self.stop(f'not used for {IDLE_STOP} s')

    # -- one model turn ---------------------------------------------------------------------------------------------------
    def generate(self, segments, max_tokens, cancelled, on_text):
        """Streams one answer. on_text(piece). Returns the 'done' event, or raises RuntimeError with a plain sentence."""
        with self.gen_lock:
            with self.lock:
                proc, events = self.proc, self.events
                if not proc or self.state != 'ready':
                    raise RuntimeError(self.message or 'The local AI is not running.')
                self.next_id += 1
                rid = self.next_id
                self.last_used = time.time()
            while not events.empty():       # anything left from an answer that was stopped
                events.get_nowait()
            req = {'op': 'gen', 'id': rid, 'segments': segments, 'max_tokens': max_tokens, 'temp': 0.2, 'top_k': 64, 'top_p': 0.95,
                   'min_p': 0.05, 'seed': 1234 + rid, 'stop': [] if LOW_VRAM else ['<|tool_response>']}
            if MINICPM:
                req.update(temp=1.0, min_p=0.0)  # publisher's recommended sampling
            try:
                proc.stdin.write((json.dumps(req, ensure_ascii=False) + '\n').encode('utf-8'))
            except OSError:
                raise RuntimeError('The local AI stopped unexpectedly. Ask again to restart it.')
            sent_cancel = False
            import queue
            while True:
                if cancelled() and not sent_cancel:
                    sent_cancel = True
                    try:
                        proc.stdin.write(b'{"op":"cancel"}\n')
                    except OSError:
                        pass
                try:
                    ev = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                kind = ev.get('ev')
                if kind == 'exit':
                    raise RuntimeError('The local AI stopped unexpectedly. Ask again to restart it.')
                if ev.get('id') != rid:
                    continue
                if kind == 'tok':
                    on_text(ev['t'])
                elif kind == 'done':
                    self.last_used = time.time()
                    return ev
                elif kind == 'error':
                    raise RuntimeError(_plain_error(ev))


LOCAL = Local()


def judge(fit):
    """Does the whole of it fit this computer? llama.cpp's fit places the model on the graphics memory it finds and counts
    the rest as the computer's own memory, which it takes to be unlimited. So the computer's own memory is checked here:
    on a Mac the two are one memory; on Windows the part placed on the graphics card is not in it."""
    total = fit.get('ram_total') or 0
    dev, host = fit.get('device_bytes') or 0, fit.get('host_bytes') or 0
    devices = [m for m in fit.get('memory') or [] if m.get('kind') == 'device']
    unified = sys.platform == 'darwin'
    # Windows: weights left in the mapped file (not on a graphics card, not repacked) are outside llama.cpp's count, yet in use
    # while answering. Measured with no GPU: the fit said 9.6 GB, the program held 16.5 to 26 GB.
    mapped = 0 if unified else max(0, SIZE - sum(m.get('model') or 0 for m in fit.get('memory') or []))
    in_ram = dev + host if unified else host + mapped
    needs = in_ram + RESERVE
    placement = [{'device': m.get('desc') or m.get('name'), 'bytes': m['model'] + m['context'] + m['compute'], 'free': m.get('free')} for m in devices]
    placement.append({'device': 'memory' if unified else 'computer memory', 'bytes': host + mapped})
    out = {'status': fit.get('status'), 'ram_total': total, 'device_bytes': dev, 'host_bytes': host, 'needs': int(needs),
           'model_bytes': dev + host, 'placement': placement, 'host_overrides': fit.get('host_overrides'), 'n_gpu_layers': fit.get('n_gpu_layers')}
    if fit.get('status') != 'fits':
        out['fits'] = False
        out['why'] = 'The model does not fit this computer’s graphics memory together with its own memory.'
    elif total and needs > total:
        out['fits'] = False
        out['why'] = (f'The model needs about {_gb(in_ram)} of this computer’s memory, plus room for everything else, '
                      f'and this computer has {_gb(total)}.')
    else:
        out['fits'] = True
    return out


# ------------------------------------------------------------------------------------------------- Gemma 4 prompt format --
Q = '<|"|>'


def _arg(v):
    """format_argument from the model's chat template, for values the app writes (tool calls it saw and parsed)."""
    if v is None:
        return [['null', True]]
    if isinstance(v, bool):
        return [['true' if v else 'false', True]]
    if isinstance(v, str):
        return [[Q, True], [v, False], [Q, True]]
    if isinstance(v, (int, float)):
        return [[str(v), True]]
    if isinstance(v, dict):
        out, first = [['{', True]], True
        for k in sorted(v):
            out += ([] if first else [[',', True]]) + [[k + ':', True]] + _arg(v[k])
            first = False
        return out + [['}', True]]
    if isinstance(v, (list, tuple)):
        out = [['[', True]]
        for i, x in enumerate(v):
            out += ([[',', True]] if i else []) + _arg(x)
        return out + [[']', True]]
    return [[str(v), False]]


def _params(props, required):
    out = []
    for i, key in enumerate(sorted(props)):
        v = props[key]
        s = (',' if i else '') + key + ':{'
        parts = []
        if v.get('description'):
            parts.append(f'description:{Q}{v["description"]}{Q}')
        t = str(v.get('type', '')).upper()
        if t == 'STRING' and v.get('enum'):
            parts.append('enum:[' + ','.join(f'{Q}{e}{Q}' for e in v['enum']) + ']')
        elif t == 'ARRAY' and isinstance(v.get('items'), dict) and v['items']:
            items = []
            for ik in sorted(v['items']):
                iv = v['items'][ik]
                if iv is None:
                    continue
                items.append(f'type:{Q}{str(iv).upper()}{Q}' if ik == 'type' else f'{ik}:{iv}')
            parts.append('items:{' + ','.join(items) + '}')
        parts.append(f'type:{Q}{t}{Q}')
        out.append(s + ','.join(parts) + '}')
    return ''.join(out)


def declaration(tool):
    f = tool['function']
    p = f.get('parameters') or {}
    s = f'declaration:{f["name"]}{{description:{Q}{f["description"]}{Q}'
    if p:
        s += ',parameters:{'
        if p.get('properties'):
            s += 'properties:{' + _params(p['properties'], p.get('required') or []) + '},'
        if p.get('required'):
            s += 'required:[' + ','.join(f'{Q}{r}{Q}' for r in p['required']) + '],'
        if p.get('type'):
            s += f'type:{Q}{p["type"].upper()}{Q}}}'
    return s + '}'


def _render_gemma(msgs, tools):
    """OpenAI-shaped messages to Gemma 4 segments, as the model's chat template lays them out, with thinking off.
    System messages before the model's first reply in this turn go together at the top, as the evaluation gave them:
    instructions, then the sources, then the question."""
    last_user = max((i for i, m in enumerate(msgs) if m['role'] == 'user'), default=-1)
    first_reply = next((i for i, m in enumerate(msgs) if i > last_user and m['role'] in ('assistant', 'tool')), len(msgs))
    top = [m for i, m in enumerate(msgs) if m['role'] == 'system' and i < first_reply]
    rest = [m for i, m in enumerate(msgs) if not (m['role'] == 'system' and i < first_reply)]
    seg = [['<|turn>system\n', True]]
    seg.append(['\n\n'.join(_text(m).strip() for m in top), False])
    for t in tools or []:
        seg += [['<|tool>', True], [declaration(t), True], ['<tool|>', True]]
    seg.append(['<turn|>\n', True])
    i = 0
    open_model = False      # inside a model turn that continues after tool responses
    while i < len(rest):
        m = rest[i]
        role = m['role']
        if role == 'assistant':
            if not open_model:
                seg.append(['<|turn>model\n', True])
            for c in m.get('tool_calls') or []:
                f = c['function']
                args = f.get('arguments')
                if isinstance(args, str):
                    try:
                        args = json.loads(args or '{}')
                    except ValueError:
                        args = {}
                seg += [['<|tool_call>call:' + f['name'] + '{', True]]
                for j, k in enumerate(sorted(args or {})):
                    seg += ([[',', True]] if j else []) + [[k + ':', True]] + _arg(args[k])
                seg.append(['}<tool_call|>', True])
            j = i + 1
            names = {c.get('id'): c['function']['name'] for c in m.get('tool_calls') or []}
            had_tools = False
            while j < len(rest) and rest[j]['role'] == 'tool':
                t = rest[j]
                name = names.get(t.get('tool_call_id'), 'unknown')
                seg += [['<|tool_response>response:' + name + '{value:' + Q, True], [_text(t), False], [Q + '}<tool_response|>', True]]
                had_tools = True
                j += 1
            content = _strip_thinking(_text(m)) if m.get('content') else ''
            if content:
                seg.append([content, False])
            nxt = rest[j]['role'] if j < len(rest) else None
            if had_tools and not content and nxt is None:
                open_model = True          # the answer continues in this same turn
            elif nxt == 'assistant' and (not m.get('tool_calls') or had_tools):
                open_model = True
            else:
                seg.append(['<turn|>\n', True])
                open_model = False
            i = j
            continue
        if role == 'tool':        # a tool answer with no call before it: keep it as plain context
            seg += [['<|turn>user\n', True], [_text(m).strip(), False], ['<turn|>\n', True]]
        else:
            seg += [['<|turn>' + role + '\n', True], [_text(m).strip(), False], ['<turn|>\n', True]]
        open_model = False
        i += 1
    if not open_model:
        seg.append(['<|turn>model\n<|channel>thought\n<channel|>', True])
    return seg


def render(msgs, tools):
    if not LOW_VRAM:
        return _render_gemma(msgs, tools)
    # ChatML formats. Source text stays in non-special segments, as for Gemma.
    top = '\n\n'.join(_text(m).strip() for m in msgs if m['role'] == 'system')
    if tools and MINICPM:
        top += ('\n\n# Tools\n\nYou are provided with function signatures within <tools></tools> XML tags:\n<tools>\n'
                + '\n'.join(json.dumps(t, ensure_ascii=False) for t in tools)
                + '\n</tools>\n\nTool usage guidelines:\n'
                '- You may call zero or more functions. If no calls are needed, answer normally and do not include function markup.\n'
                '- When calling a function, return an XML object using:\n'
                '<function name="function-name"><param name="param-name">param-value</param></function>\n'
                '- param-value may be multi-line. If it contains <, & or newline characters, wrap it in a CDATA block: '
                '<param name="param-name"><![CDATA[...multi-line value...]]></param>')
    elif tools and QWEN35:
        top = ('# Tools\n\nYou have access to the following functions:\n\n<tools>\n'
               + '\n'.join(json.dumps(t, ensure_ascii=False) for t in tools)
               + '\n</tools>\n\nIf you choose to call a function ONLY reply in the following format with NO suffix:\n\n'
               '<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>\nvalue_1\n'
               '</parameter>\n</function>\n</tool_call>\n\n'
               'Function calls MUST use an inner <function=...></function> block inside <tool_call></tool_call>. '
               'Required parameters MUST be specified. You may explain a call BEFORE it, but NOT after. '
               'When no call is needed, answer normally from the supplied sources.\n\n' + top)
    elif tools:
        top += ('\n\n# Tools\nYou may call one or more functions to assist with the user query.\n'
                'You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n'
                + '\n'.join(json.dumps(t, ensure_ascii=False) for t in tools)
                + '\n</tools>\nFor each function call, return a json object with function name and arguments '
                'within <tool_call></tool_call> XML tags:\n<tool_call>\n'
                '{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>')
    seg = [['<|im_start|>system\n', True], [top, False], ['<|im_end|>\n', True]]
    rest = [m for m in msgs if m['role'] != 'system']
    for i, m in enumerate(rest):
        role = m['role']
        if role == 'tool':
            if i == 0 or rest[i - 1]['role'] != 'tool':
                seg.append(['<|im_start|>user', True])
            seg += [['\n<tool_response>\n', True], [_text(m), False], ['\n</tool_response>', True]]
            if i + 1 == len(rest) or rest[i + 1]['role'] != 'tool':
                seg.append(['<|im_end|>\n', True])
            continue
        if role not in ('user', 'assistant'):
            continue
        history_prefix = f'<|im_start|>{role}\n'
        if role == 'assistant' and MINICPM:
            history_prefix += '<think>\n\n</think>\n\n'
        seg += [[history_prefix, True], [_text(m), False]]
        for call in m.get('tool_calls') or []:
            f = call['function']
            args = f.get('arguments') or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if MINICPM:
                seg.append(['\n<function name="' + f['name'] + '">', True])
                for key, value in args.items():
                    value = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                    value = '<![CDATA[' + value.replace(']]>', ']]]]><![CDATA[>') + ']]>'
                    seg += [['<param name="' + key + '">', True], [value, False], ['</param>', True]]
                seg.append(['</function>', True])
            elif QWEN35:
                seg.append(['\n<tool_call>\n<function=' + f['name'] + '>\n', True])
                for key, value in args.items():
                    value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                    seg += [['<parameter=' + key + '>\n', True], [value, False], ['\n</parameter>\n', True]]
                seg.append(['</function>\n</tool_call>', True])
            else:
                seg += [['\n<tool_call>\n', True],
                        [json.dumps({'name': f['name'], 'arguments': args}, ensure_ascii=False), False],
                        ['\n</tool_call>', True]]
        seg.append(['<|im_end|>\n', True])
    prefix = '<|im_start|>assistant\n' + ('<think>\n\n</think>\n\n' if QWEN35 or MINICPM else '')
    return seg + [[prefix, True]]


def visible_text(text):
    """Hide calls, including an opening tag arriving across multiple streamed tokens."""
    text = text.split(TOOL_MARKER, 1)[0]
    for n in range(len(TOOL_MARKER) - 1, 0, -1):
        if text.endswith(TOOL_MARKER[:n]):
            return text[:-n].lstrip()
    return text.lstrip()


def _text(m):
    c = m.get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return ' '.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text')
    return ''


def _strip_thinking(text):
    return re.sub(r'<\|channel>.*?<channel\|>', '', text, flags=re.S).strip()


def size_of(segments):
    """A rough count of tokens, on the high side: about 3.2 characters a token for English text."""
    return sum(len(s) for s, _ in segments) / 3.2 + 8 * len(segments)


# ------------------------------------------------------------------------------------------------- Gemma's tool calls --
CALL = re.compile(r'<\|tool_call>call:([A-Za-z_][\w.-]*)\{(.*?)\}<tool_call\|>', re.S)


def parse_calls(text):
    """(text before the first call, [(name, arguments dict)]) from what the model wrote."""
    if MINICPM:
        calls = []
        for body in re.findall(r'<function\s+name=[\"\'].*?</function>', text, re.S):
            try:
                node = ET.fromstring(body)
            except ET.ParseError:
                continue
            name = node.get('name', '')
            if not re.fullmatch(r'[A-Za-z_][\w.-]*', name):
                continue
            args = {}
            for param in node:
                key = param.get('name', '')
                if param.tag != 'param' or not re.fullmatch(r'[A-Za-z_][\w.-]*', key) or len(param):
                    break
                value = (param.text or '').strip()
                try:
                    args[key] = json.loads(value)
                except ValueError:
                    args[key] = value
            else:
                calls.append((name, args))
        return text.split(TOOL_MARKER, 1)[0], calls
    if LOW_VRAM:
        calls = []
        for body in re.findall(r'<tool_call>(.*?)</tool_call>', text, re.S):
            if QWEN35:
                match = re.fullmatch(r'\s*<function=([A-Za-z_][\w.-]*)>(.*?)</function>\s*', body, re.S)
                if not match:
                    continue
                args = {}
                for key, value in re.findall(r'<parameter=([A-Za-z_][\w.-]*)>(.*?)</parameter>', match[2], re.S):
                    value = value.strip()
                    try:
                        args[key] = json.loads(value)
                    except ValueError:
                        args[key] = value
                calls.append((match[1], args))
                continue
            try:
                call = json.loads(body)
                if isinstance(call, dict) and isinstance(call.get('name'), str) and isinstance(call.get('arguments'), dict):
                    calls.append((call['name'], call['arguments']))
            except ValueError:
                continue
        return text.split(TOOL_MARKER, 1)[0], calls
    calls = []
    for m in CALL.finditer(text):
        try:
            args = _Args(m.group(2)).obj_body()
        except ValueError:
            args = {}
        calls.append((m.group(1), args))
    cut = text.find('<|tool_call>')
    return (text if cut < 0 else text[:cut]), calls


class _Args:
    """Reads Gemma's argument notation: key:value pairs, strings between <|"|> marks, numbers, true/false/null, [..] and {..}."""

    def __init__(self, s):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in ' \n\t\r':
            self.i += 1

    def obj_body(self, end=None):
        out = {}
        while True:
            self.ws()
            if self.i >= len(self.s) or (end and self.s[self.i] == end):
                return out
            if self.s.startswith(Q, self.i):
                key = self.string()
            else:
                k = re.compile(r'[A-Za-z_][\w-]*').match(self.s, self.i)
                if not k:
                    raise ValueError(self.s[self.i:self.i + 20])
                key, self.i = k.group(0), k.end()
            self.ws()
            if not self.s.startswith(':', self.i):
                raise ValueError('no colon')
            self.i += 1
            out[key] = self.value()
            self.ws()
            if self.s.startswith(',', self.i):
                self.i += 1

    def string(self):
        self.i += len(Q)
        j = self.s.find(Q, self.i)
        if j < 0:
            raise ValueError('open string')
        v, self.i = self.s[self.i:j], j + len(Q)
        return v

    def value(self):
        self.ws()
        s = self.s
        if s.startswith(Q, self.i):
            return self.string()
        if s.startswith('{', self.i):
            self.i += 1
            v = self.obj_body('}')
            self.i += 1
            return v
        if s.startswith('[', self.i):
            self.i += 1
            out = []
            while True:
                self.ws()
                if s.startswith(']', self.i):
                    self.i += 1
                    return out
                out.append(self.value())
                self.ws()
                if s.startswith(',', self.i):
                    self.i += 1
        m = re.compile(r'[^,}\]]+').match(s, self.i)
        if not m:
            raise ValueError('no value')
        self.i = m.end()
        w = m.group(0).strip()
        if w in ('true', 'false'):
            return w == 'true'
        if w == 'null':
            return None
        try:
            return int(w)
        except ValueError:
            try:
                return float(w)
            except ValueError:
                return w.strip('"')


# ------------------------------------------------------------------------------------------------------------ helpers --
def _gb(n):
    return f'{n / 1e9:.1f} GB'


def _folder_name(p):
    p = Path(p)
    home = Path.home()
    folder = p.parent if p.suffix == '.gguf' else p
    try:
        rel = folder.relative_to(home)
        return str(rel) if str(rel) != '.' else 'your home folder'
    except ValueError:
        return folder.name or str(folder)


def _plain_error(ev):
    code = ev.get('code')
    return {'too_long': 'The question and its sources were too long for the local AI. Start a new chat or ask a shorter question.',
            'load': 'The local AI could not load the model file.',
            'context': 'There was not enough free memory to start the local AI. Close other programs and ask again.',
            'model_file': 'The local AI could not read the model file.'}.get(code, ev.get('msg') or 'The local AI failed.')


def _quiet():
    """No console window for the program on Windows."""
    return {'creationflags': 0x08000000} if os.name == 'nt' else {}     # CREATE_NO_WINDOW


_job = None


def _tie_to_this_process(proc):
    """Windows: the program goes when Lastbook goes, even if Lastbook is killed (a job object that kills on close).
    On a Mac it leaves when its standard input closes, which happens when Lastbook ends for any reason."""
    global _job
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        if _job is None:
            job = k32.CreateJobObjectW(None, None)

            class LIMIT(ctypes.Structure):
                _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                            ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                            ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ('a', 'b', 'c', 'd', 'e', 'f')]

            class EXT(ctypes.Structure):
                _fields_ = [('Basic', LIMIT), ('Io', IO), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                            ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
            info = EXT()
            info.Basic.LimitFlags = 0x2000      # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
            _job = job
        h = k32.OpenProcess(0x0101, False, proc.pid)    # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        k32.AssignProcessToJobObject(wintypes.HANDLE(_job), wintypes.HANDLE(h))
        k32.CloseHandle(wintypes.HANDLE(h))
    except Exception as e:
        print('local AI: could not tie the program to Lastbook:', e, flush=True)
