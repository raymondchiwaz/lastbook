#!/usr/bin/env python3
"""Exercise a real GGUF with a tiny generated library. Does not download any files.

Run with .venv-low-vram/bin/python tools/smoke_low_vram.py --model-dir /path/to/models
Add --model qwen3.5-0.8b or --model qwen2.5-1.5b to check the other profiles.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model-dir', required=True)
    ap.add_argument('--model', choices=['qwen3.5-2b', 'qwen3.5-0.8b', 'qwen2.5-1.5b', 'qwen3.5-2b-abliterated', 'minicpm5-2b'], default='qwen3.5-2b')
    ap.add_argument('--gpu', action='store_true', help='use auto-fitting instead of the default CPU check')
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix='lastbook-model-check-') as work:
        os.environ.update(LASTBOOK_AI_PROFILE='low-vram', LASTBOOK_AI_MODEL=args.model,
                          LASTBOOK_MODEL_DIR=str(Path(args.model_dir).resolve()), LASTBOOK_DATA=work,
                          LASTBOOK_LOCAL_AI='1', SURVIVAL_DISABLE_INDEX_WATCH='1')
        os.environ['LASTBOOK_AI_CPU_ONLY'] = '0' if args.gpu else '1'
        lib = Path(work) / 'library-data'
        (lib / 'documents').mkdir(parents=True)
        text = 'Archive test record. The vault identifier is MARULA-73. The archive label is violet.'
        (lib / 'documents/vault.txt').write_text(text)
        record = {'id': 'vault', 'kind': 'text', 'path': 'documents/vault.txt', 'text_path': 'documents/vault.txt',
                  'title': 'Archive test record', 'publisher': 'Test fixture', 'category': 'test', 'collection': 'Test', 'pages': 1}
        (lib / 'catalog.json').write_text(json.dumps([record]))
        (lib / 'summary.json').write_text('{"resources":1}')
        with sqlite3.connect(lib / 'search.sqlite') as con:
            con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body)')
            con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('vault', record['title'], 'Test', 'test', record['path'], 1, text))
        import server
        import assistant
        import llm_local
        server.init(no_index=True)
        local = llm_local.LOCAL
        try:
            deadline = time.monotonic() + 90
            while not local.available() and time.monotonic() < deadline:
                if local.state in ('error', 'wrong', 'too_big', 'off'):
                    break
                time.sleep(0.1)
            if not local.available():
                raise RuntimeError(json.dumps(local.status()))
            events = []
            assistant.chat([{'role': 'user', 'content': 'What is the vault identifier on this page? Include the source citation.'}],
                           'doc:vault:1', events.append, lambda: False)
            done = next((e for e in events if e['type'] == 'done'), None)
            if not done or 'MARULA-73' not in done['answer'] or not done.get('sources') or done.get('invalid'):
                raise RuntimeError('Library answer failed: ' + json.dumps(events, ensure_ascii=False))
            print('CITED_ANSWER', json.dumps(done, ensure_ascii=False), flush=True)
            pieces = []
            tools = [{'type': 'function', 'function': {'name': 'search_library', 'description': 'Search the offline library',
                      'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}]
            msgs = [{'role': 'system', 'content': 'You must use the search_library tool before answering. Do not guess.'},
                    {'role': 'user', 'content': 'Search the library for rainwater storage.'}]
            event = local.generate(llm_local.render(msgs, tools), 256, lambda: False, pieces.append)
            output = ''.join(pieces)
            calls = llm_local.parse_calls(output)[1]
            if not calls or calls[0][0] != 'search_library' or not isinstance(calls[0][1].get('query'), str):
                raise RuntimeError('Tool call failed: ' + output)
            print('TOOL_CALL', json.dumps({'calls': calls, 'done': event}), flush=True)
            # Continue a real tool turn: the model must consume the tool result and answer with its source.
            follow = [
                {'role': 'system', 'content': assistant.SYSTEM_TEXT_ONLY},
                {'role': 'user', 'content': 'What is the archive label? Search the archive first and include the source citation in square brackets.'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'archive_search', 'type': 'function',
                    'function': {'name': 'search_library', 'arguments': '{"query":"archive label"}'}}]},
                {'role': 'tool', 'tool_call_id': 'archive_search',
                 'content': assistant._src_block('S1', dict(record, doc_id='vault', trust='Reference'), text)},
            ]
            pieces = []
            follow_done = local.generate(llm_local.render(follow, tools), 256, lambda: False, pieces.append)
            follow_answer = ''.join(pieces)
            if 'violet' not in follow_answer.lower() or '[S1]' not in follow_answer or llm_local.parse_calls(follow_answer)[1]:
                raise RuntimeError('Tool-result continuation failed: ' + follow_answer)
            print('TOOL_RESULT_ANSWER', json.dumps({'answer': follow_answer, 'done': follow_done}), flush=True)
            print('PASS', json.dumps({'model': llm_local.NAME, 'fit': local.fit, 'ready': local.ready_info}), flush=True)
        finally:
            local.stop('smoke check complete')


if __name__ == '__main__':
    main()
