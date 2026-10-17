# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Configuration validation and paths relative to the installed project."""
from __future__ import annotations
import shutil
from pathlib import Path
from src.contracts.contracts import jsonc, strict_json
from src.runtime.execution import execution_settings
from src.reports.architecture_format import resolve_language
from src.analysis.source_decoding import normalize_source_decoding
from src.analysis.source_context import source_context_settings
from src.analysis.source_filter import normalize_source_filter
from src.runtime.errors import AuditError
from src.runtime.artifacts import inside, overlap

ROOT = Path(__file__).resolve().parents[2]
STAGES = ('study', 'review', 'compare')
SOURCE_STAGES = ('study', 'review')
BACKENDS = {'codex': 'codex', 'opencode': 'opencode', 'xxx': 'xxx'}

def resolve_agent(agent: dict, path: Path) -> dict:
    agent = dict(agent)
    if set(agent) - {'backend', 'executable', 'model', 'reasoning_effort', 'expected_version'}:
        raise AuditError('Unknown agent configuration field.')
    backend = agent.get('backend')
    if backend not in BACKENDS:
        raise AuditError(f'Unsupported backend: {backend}')
    effort = agent.get('reasoning_effort')
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise AuditError('agent.reasoning_effort must be a nonempty string or null.', code='INVALID_CONFIG')
    if backend == 'opencode' and effort is not None:
        if '#' in effort:
            raise AuditError('OpenCode reasoning_effort cannot contain "#".', code='INVALID_CONFIG')
        model = agent.get('model')
        if not isinstance(model, str):
            raise AuditError('OpenCode reasoning_effort requires agent.model in provider/model form.', code='INVALID_CONFIG')
        selector, suffix, variant = model.partition('#')
        provider, slash, name = selector.partition('/')
        if not slash or not provider.strip() or not name.strip():
            raise AuditError('OpenCode reasoning_effort requires agent.model in provider/model form.', code='INVALID_CONFIG')
        if suffix and variant != effort:
            raise AuditError('OpenCode model variant conflicts with agent.reasoning_effort.', code='INVALID_CONFIG')
    executable_name = agent.get('executable') or BACKENDS[backend]
    if not isinstance(executable_name, str):
        raise AuditError('agent.executable must be a string.')
    if '/' in executable_name:
        executable_path = Path(executable_name).expanduser()
        executable_name = str((path.resolve().parent / executable_path).resolve())
    executable = shutil.which(executable_name)
    if not executable:
        raise AuditError(f'{backend} executable was not found. Install it separately.')
    agent['executable'] = str(Path(executable).resolve())
    for key in ('model', 'expected_version'):
        if agent.get(key) is not None and not isinstance(agent[key], str):
            raise AuditError(f'agent.{key} must be a string or null.')
    return agent

def load_config(path: Path) -> dict:
    path = path.resolve()
    value = (jsonc if path.suffix.lower() == '.jsonc' else strict_json)(path.read_text())
    if type(value) is not dict:
        raise AuditError('Configuration must be a JSON object.')
    allowed = {'reports_dir', 'agent', 'stage_agents',
        'output_language', 'priority_scenarios', 'continue_on_error', 'project_description', 'prompts',
        'mode', 'git_mode', 'folder_mode', 'execution', 'result_policy', 'source_decoding',
        'source_filter', 'source_context'}
    if set(value) - allowed:
        raise AuditError(f'Unknown configuration keys: {set(value) - allowed}')
    try:
        if 'execution' in value and value['execution'] is None:
            raise ValueError('execution must be a JSON object.')
        if 'source_context' in value and value['source_context'] is None:
            raise ValueError('source_context must be a JSON object.')
        value['execution'] = execution_settings(value.get('execution'))
        value['source_context'] = source_context_settings(value.get('source_context'))
        value['source_decoding'] = normalize_source_decoding(value.get('source_decoding'))
        value['source_filter'] = normalize_source_filter(value.get('source_filter', {}))
    except ValueError as exc:
        raise AuditError(str(exc), code='INVALID_CONFIG') from exc
    for key in ('mode', 'reports_dir', 'agent'):
        if key not in value:
            raise AuditError(f'Missing configuration key: {key}')
    mode = value['mode']
    if mode not in ('git', 'folder'):
        raise AuditError('Configuration requires mode: "git" or "folder".')
    for section in ('git_mode', 'folder_mode'):
        if section in value and type(value[section]) is not dict:
            raise AuditError(f'{section} must be a JSON object, even when inactive.')
    section = mode + '_mode'
    if section not in value:
        raise AuditError(f'Missing configuration section: {section}')
    source = value[section]
    required = ('repository', 'branches', 'baseline_branch') if mode == 'git' else ('path',)
    if set(source) - set(required):
        raise AuditError(f'Unknown {mode}_mode keys: {set(source) - set(required)}')
    for key in required:
        if key not in source:
            raise AuditError(f'Missing configuration key: {key}')
    source_key = 'repository' if mode == 'git' else 'path'
    for obj, key in ((source, source_key), (value, 'reports_dir')):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise AuditError(f'{key} must be a nonempty path string.')
        p = Path(obj[key]).expanduser()
        obj[key] = str((path.parent / p if not p.is_absolute() else p).resolve())
    repo, reports = Path(source[source_key]), Path(value['reports_dir'])
    project_reports = (reports / repo.name).resolve()
    if overlap(repo, reports) or overlap(repo, project_reports):
        raise AuditError('Source directory and reports_dir must be disjoint, not ancestors of each other.')
    value['_reports_dir'] = str(project_reports)
    if mode == 'git':
        branches = source['branches']
        if not isinstance(branches, list) or not branches or any(not isinstance(b, str) or not b for b in branches):
            raise AuditError('At least one nonempty local branch name is required.')
        if len(set(branches)) != len(branches) or source['baseline_branch'] not in branches:
            raise AuditError('Branches must be unique and include baseline_branch.')
    defaults = {'output_language': 'Russian', 'project_description': '', 'priority_scenarios': [],
        'continue_on_error': True, 'stage_agents': {}, 'prompts': {}, 'result_policy': 'compromise'}
    for key, default in defaults.items():
        value.setdefault(key, default)
    try:
        resolve_language(value['output_language'])
    except ValueError as exc:
        raise AuditError(str(exc), code='INVALID_CONFIG') from exc
    if not isinstance(value['project_description'], str):
        raise AuditError('project_description must be a string.')
    value['project_description'] = value['project_description'].strip()
    for key in ('agent', 'stage_agents', 'prompts'):
        if type(value[key]) is not dict:
            raise AuditError(f'{key} must be a JSON object.')
    if type(value['continue_on_error']) is not bool:
        raise AuditError('continue_on_error must be boolean.')
    if value['result_policy'] not in ('compromise', 'strict'):
        raise AuditError('result_policy must be compromise or strict.')
    if not isinstance(value['priority_scenarios'], list) or any(not isinstance(s, str) for s in value['priority_scenarios']):
        raise AuditError('priority_scenarios must be an array of strings.')
    stages = STAGES if mode == 'git' and len(source['branches']) > 1 else SOURCE_STAGES
    if not value['execution']['review_enabled']:
        stages = tuple(stage for stage in stages if stage != 'review')
    for section in ('stage_agents', 'prompts'):
        if 'catalog' in value[section]:
            raise AuditError(f'Remove {section}.catalog: the Catalog stage has been removed.', code='INVALID_CONFIG')
    for stage in value['stage_agents']:
        if stage not in STAGES:
            raise AuditError(f'Unknown stage override: {stage}')
        if stage in stages and type(value['stage_agents'][stage]) is not dict:
            raise AuditError(f'stage_agents.{stage} must be a JSON object.')
    value['_agents'] = {}
    for stage in ('study', *(s for s in stages if s != 'study')):
        agent = dict(value['agent'])
        agent.update(value['stage_agents'].get(stage, {}))
        value['_agents'][stage] = resolve_agent(agent, path)
    value['_prompt_paths'] = {}
    if set(value['prompts']) - (set(STAGES) | {'revise'}):
        raise AuditError('Unknown prompt stage.')
    for stage in (*stages, *(('revise',) if value['execution']['review_enabled'] and value['execution']['max_revision_rounds'] else ())):
        prompt_path = value['prompts'].get(stage, str(ROOT / 'prompts' / (stage + '.md')))
        if not isinstance(prompt_path, str) or not prompt_path.strip():
            raise AuditError(f'prompts.{stage} must be a nonempty path string.')
        p = Path(prompt_path).expanduser()
        p = (path.parent / p if not p.is_absolute() else p).resolve()
        if inside(p, repo):
            raise AuditError('Prompt templates must be outside the inspected repository.')
        if not p.is_file():
            raise AuditError(f'Missing prompt template: {p}')
        value['_prompt_paths'][stage] = str(p)
    return value
