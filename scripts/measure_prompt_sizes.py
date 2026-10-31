#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Offline input sizes on explicit synthetic contexts. No token/quality claims."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import platform
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts.contracts import MODEL_FOLDER_SCHEMAS, MODEL_FOLDER_STUDY_PHASE_SCHEMAS
from src.analysis.evidence import sha
from src.analysis.folder import Folder
from src.analysis.source_context import SourceContextBuilder, parse_symbols
from src.mcp.service import prepare_manifest
from src.model.model_context import (build_model_prompt, input_measurements,
                                     mcp_source_index, on_demand_source_index, project_model_context, serialize_model_input)
from src.runtime.artifacts import save_json
from src.runtime.tool_observer import ToolObserver


def synthetic_source(number, symbols_per_file=2, name_length=0):
    if symbols_per_file != 2 or name_length:
        return ''.join(f'def symbol{number:05d}_{symbol:05d}' + 'x' * name_length +
                       '():\n    return 1\n' for symbol in range(symbols_per_file))
    return (f'def entry{number:05d}():\n    return helper{number:05d}()\n\n'
            f'def helper{number:05d}():\n    return {number}\n')


def synthetic_index(count, symbols_per_file=2, name_length=0):
    files, source_bytes = [], 0
    for number in range(count):
        path = f'src/file{number:05d}.py'
        source = synthetic_source(number, symbols_per_file, name_length)
        source_bytes += len(source.encode('utf-8'))
        files.append(dict(source_id='source-001', path=path,
                          **parse_symbols(path, source), encoding='utf-8',
                          total_lines=source.count('\n')))
    return {'mode': 'index-only', 'files': files, 'fragments': [],
            'omissions': {'maps': 0, 'reasons': {}, 'incomplete': False}}, source_bytes


def legacy_index(index):
    result = copy.deepcopy(index)
    result['file_columns'] = ['source_id', 'path', 'parser', 'language', 'encoding', 'total_lines',
                              'map_status', 'map_reason', 'symbols']
    result['symbol_columns'] = ['name', 'kind', 'parent', 'start_line', 'end_line']
    result['files'] = [[file[column] if column != 'symbols' else
                       [[symbol[key] for key in result['symbol_columns']] for symbol in file['symbols']]
                       for column in result['file_columns']] for file in result['files']]
    return result


def measurements():
    samples = []
    cases = [('many-files', count, 2, 0) for count in (1, 1000, 5000)]
    cases += [('many-symbols', 1, 10000, 0), ('long-symbol-names', 1, 100, 1500)]
    for case, count, symbols_per_file, name_length in cases:
        index, source_bytes = synthetic_index(count, symbols_per_file, name_length)
        paths = [file['path'] for file in index['files']]
        stages = {}
        for stage in ('study', 'align', 'review'):
            task = 'study' if stage == 'align' else stage
            context = {'stage': task, 'source_mode': 'folder', 'source_directory': '/synthetic/source',
                       'source_snapshot_id': 'synthetic-snapshot', 'output_language': 'English',
                       'priority_scenarios': [], '_inventory': [{'path': path} for path in paths],
                       'source_context': index,
                       'coverage_plan': {'areas': [{'id': 'S-001', 'paths': ['src'],
                           'entry_paths': paths, 'file_paths': paths}], 'exclusions': []}}
            if stage == 'align':
                context['prompt_variant'] = 'align'
            if stage == 'study':
                context['inventory_summary'] = {'files': count, 'symlinks': 0, 'directory_count': 2,
                    'empty_directories': 0, 'directories': [
                        {'path': '.', 'files': 0, 'symlinks': 0, 'directories': 1, 'empty_directories': 0},
                        {'path': 'src', 'files': count, 'symlinks': 0, 'directories': 0, 'empty_directories': 0}]}
            projected = project_model_context(task, context)
            schema_json = serialize_model_input(MODEL_FOLDER_SCHEMAS[task])
            template = (ROOT / 'prompts' / (stage + '.md')).read_text(encoding='utf-8')
            legacy = legacy_index(index)
            baseline_prompt = build_model_prompt(template,
                serialize_model_input(dict(projected, source_context=legacy)), schema_json, source_index=True)
            baseline_bytes = len(baseline_prompt.encode('utf-8'))
            stages[stage] = {'legacy_rows_index_utf8_bytes': len(serialize_model_input(legacy).encode('utf-8')),
                             'legacy_rows_prompt_utf8_bytes': baseline_bytes}
            demand, maps = on_demand_source_index(projected['source_context'], (ROOT / 'synthetic-maps').resolve())
            map_bytes = sum(len((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
                            for value in maps.values())
            for delivery, wire, stored in (('full', projected['source_context'], 0), ('on-demand', demand, map_bytes)):
                context_json = serialize_model_input(dict(projected, source_context=wire))
                prompt = build_model_prompt(template, context_json, schema_json, source_index=True)
                measured = input_measurements(template, context_json, schema_json, prompt,
                    source_context=index, transmitted_source_context=wire, stored_map_utf8_bytes=stored)
                transmitted_bytes = len(prompt.encode('utf-8'))
                saved_bytes = baseline_bytes - transmitted_bytes
                measured['legacy_rows_comparison'] = {'saved_utf8_bytes': saved_bytes,
                    'reduction_percent': round(saved_bytes * 100 / baseline_bytes, 2)}
                stages[stage][delivery] = measured
        samples.append({'case': case, 'files': count, 'symbols': sum(len(file['symbols']) for file in index['files']),
                        'source_utf8_bytes': source_bytes, 'stages': stages})
    return {'fixture': 'synthetic size samples, not runnable investigation contexts or live usage measurements',
            'backend': 'codex', 'samples': samples}


async def _mcp_workload(manifest_path, manifest_hash, context):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    observer = ToolObserver('codex', context=context)
    operations = {}
    parameters = StdioServerParameters(command=sys.executable, args=[
        '-I', '-B', str(ROOT / 'src/mcp/server.py'), '--manifest', str(manifest_path), '--sha256', manifest_hash])
    started = time.perf_counter()
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            startup_seconds = time.perf_counter() - started
            sequence = 0
            async def call(tool, arguments):
                nonlocal sequence
                before = time.perf_counter()
                result = await session.call_tool(tool, arguments)
                elapsed = time.perf_counter() - before
                sequence += 1
                wire = result.model_dump(mode='json', by_alias=True, exclude_none=True)
                payload = json.loads(result.content[0].text)
                if 'error' in payload:
                    raise RuntimeError(payload['error'])
                metric = operations.setdefault(tool, dict(calls=0, seconds=0, result_json_utf8_bytes=0,
                                                          text_content_utf8_bytes=0))
                metric['calls'] += 1
                metric['seconds'] += elapsed
                metric['result_json_utf8_bytes'] += len(result.model_dump_json(by_alias=True, exclude_none=True).encode('utf-8'))
                metric['text_content_utf8_bytes'] += len(result.content[0].text.encode('utf-8'))
                observer.event({'type': 'item.completed', 'item': dict(id=str(sequence), type='mcp_tool_call',
                    server='explain_this_pls', tool=tool, arguments=arguments, status='completed', result=wire)})
                return payload
            async def pages(tool, arguments):
                rows, cursor = [], None
                while True:
                    result = await call(tool, dict(arguments, limit=1000, cursor=cursor))
                    rows.extend(result['results'])
                    if result.get('errors'):
                        raise RuntimeError(result['errors'])
                    if not result['has_more']:
                        return rows
                    cursor = result['cursor']
            files = await pages('list_files', {})
            matches = await pages('search_text', {'query': 'return helper'})
            found = await call('search_symbols', {'query': 'entry00000'})
            symbol = found['results'][0]
            await call('read_symbol', {'symbol_id': symbol['symbol_id']})
            await call('read_source', dict(source_id=symbol['source_id'], path=symbol['path'],
                start_line=symbol['start_line'], end_line=symbol['end_line']))
    observer.event({'type': 'turn.completed'})
    metrics = observer.finish()
    return dict(startup_seconds=startup_seconds, operations=operations, enumerated_files=len(files),
                unique_file_paths=len({(row['source_id'], row['path']) for row in files}), matches=len(matches),
                result_json_utf8_bytes=sum(value['result_json_utf8_bytes'] for value in operations.values()),
                text_content_utf8_bytes=sum(value['text_content_utf8_bytes'] for value in operations.values()),
                source_text_utf8_bytes=metrics['mcp']['text_utf8_bytes'], unique_source_lines=metrics['unique_lines'],
                repeated_source_lines=metrics['repeated_lines'])


def mcp_measurements(counts=(1000, 5000)):
    samples = []
    for count in counts:
        with tempfile.TemporaryDirectory(prefix='measure-mcp-') as raw:
            base = Path(raw).resolve()
            root, attempt = base / 'source', base / 'attempt'
            (root / 'src').mkdir(parents=True)
            for number in range(count):
                (root / f'src/file{number:05d}.py').write_text(synthetic_source(number), encoding='utf-8')
            folder = Folder(root)
            inventory = folder.snapshot()
            context = dict(source_mode='folder', source_directory=str(root),
                           source_fingerprint=inventory['source_fingerprint'], _inventory=inventory)
            index, preparation = SourceContextBuilder().prepare('study', context, expected_metadata=folder.metadata)
            context['source_context'] = index
            packed, maps = on_demand_source_index(index, attempt / 'source-maps')
            descriptors = {}
            for name, value in maps.items():
                path = attempt / 'source-maps' / name
                save_json(path, value)
                blob = path.read_bytes()
                descriptors[name] = dict(path=str(path), bytes=len(blob), sha256=sha(blob))
            manifest_path = attempt / 'mcp/manifest.json'
            manifest = prepare_manifest(context, index, packed, descriptors, attempt_id='measurement',
                                        expected_metadata=folder.metadata)
            save_json(manifest_path, manifest)
            manifest_hash = sha(manifest_path.read_bytes())
            context['_mcp_source_access'] = dict(server='explain_this_pls', attempt_id='measurement',
                manifest_sha256=manifest_hash, receipts_path=str(manifest_path.parent / 'served.jsonl'))
            # The previous MCP projection sent all file rows, without private map references.
            file_list = copy.deepcopy(packed)
            column = file_list['file_columns'].index('symbols_ref')
            file_list['file_columns'].pop(column)
            for row in file_list['files']:
                row.pop(column)
            file_list.pop('maps_root')
            file_list['delivery'] = 'mcp'
            overview = mcp_source_index(packed, manifest['catalog'])
            stages = {}
            for stage in ('collect', 'review', 'align'):
                task = 'review' if stage == 'review' else 'study'
                prompt_context = dict(stage=task, source_mode='folder', source_directory='/synthetic/source',
                    source_snapshot_id='measurement', output_language='English', priority_scenarios=[],
                    coverage_plan={'areas': [{'id': 'S-001', 'paths': ['src'], 'entry_count': count, 'file_count': count}]})
                if stage == 'collect':
                    prompt_context['phase'] = 'collect'
                if stage == 'align':
                    prompt_context['prompt_variant'] = 'align'
                schema = MODEL_FOLDER_STUDY_PHASE_SCHEMAS['collect'] if stage == 'collect' else MODEL_FOLDER_SCHEMAS[task]
                schema_json = serialize_model_input(schema)
                template = (ROOT / 'prompts' / (stage + '.md')).read_text(encoding='utf-8')
                sizes = {}
                for name, source in (('file_list', file_list), ('overview', overview)):
                    context_json = serialize_model_input(dict(prompt_context, source_context=source))
                    prompt = build_model_prompt(template, context_json, schema_json, source_index=True, source_access='mcp')
                    sizes[name] = dict(source_context_utf8_bytes=len(serialize_model_input(source).encode('utf-8')),
                                       prompt_utf8_bytes=len(prompt.encode('utf-8')))
                stages[stage] = sizes
            workload = asyncio.run(_mcp_workload(manifest_path, manifest_hash, context))
            folder.assert_snapshot(inventory['source_fingerprint'])
            if workload['enumerated_files'] != count or workload['unique_file_paths'] != count or workload['matches'] != count:
                raise RuntimeError('MCP pagination lost inventory records or matches.')
            samples.append(dict(files=count, source_utf8_bytes=sum(entry['size'] for entry in inventory['entries']
                if entry['type'] == 'file'), preparation_seconds=preparation['duration_seconds'], stages=stages, mcp=workload))
    return dict(fixture='Synthetic Python files, two declarations per file; no model calls.',
        comparison='Current templates, schemas and instructions held constant; full file rows versus overview.',
        workload='Enumerate all files; find one literal per file; find/read one declaration; reread its two lines with read_source.',
        byte_scope='MCP CallToolResult JSON bytes, including text and structured content; excludes JSON-RPC envelopes, initialization and tool schemas.',
        timing_scope='One SDK stdio run per project; tool times include integrity checks and transport. No cold-cache control.',
        python=platform.python_version(), platform=platform.platform(), samples=samples)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mcp-only', action='store_true', help='Measure MCP file overview and real SDK stdio reads on 1,000 and 5,000 files.')
    args = parser.parse_args()
    print(json.dumps(mcp_measurements() if args.mcp_only else measurements(), ensure_ascii=False, indent=2))
