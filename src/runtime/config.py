# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Configuration validation and paths relative to the installed project."""
from __future__ import annotations
import datetime as dt
import shutil
from pathlib import Path
from src.messages import message as ui_message
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
BACKENDS = {'codex': 'codex'}

def source_access_settings(value=None):
    if value is None:
        value = {}
    if type(value) is not dict or set(value) - {'mode'}:
        raise ValueError(ui_message('diagnostic.config.source_access_accepts_mode_only'))
    mode = value.get('mode', 'native')
    if mode not in ('native', 'mcp'):
        raise ValueError(ui_message('diagnostic.config.source_access_mode_must_be_native_or_mcp'))
    return {'mode': mode}

def resolve_agent(agent: dict, path: Path) -> dict:
    agent = dict(agent)
    if set(agent) - {'backend', 'executable', 'model', 'reasoning_effort', 'expected_version'}:
        raise AuditError(ui_message('diagnostic.config.unknown_agent_configuration_field'))
    backend = agent.get('backend')
    if backend not in BACKENDS:
        raise AuditError(ui_message('diagnostic.config.unsupported_backend', backend=backend))
    effort = agent.get('reasoning_effort')
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise AuditError(ui_message('diagnostic.config.agent_reasoning_effort_must_be_a_nonempty_string'), code='INVALID_CONFIG')
    executable_name = agent.get('executable') or BACKENDS[backend]
    if not isinstance(executable_name, str):
        raise AuditError(ui_message('diagnostic.config.agent_executable_must_be_a_string'))
    if '/' in executable_name:
        executable_path = Path(executable_name).expanduser()
        executable_name = str((path.resolve().parent / executable_path).resolve())
    executable = shutil.which(executable_name)
    if not executable:
        raise AuditError(ui_message('diagnostic.config.executable_was_not_found_install_it_separately', backend=backend))
    agent['executable'] = str(Path(executable).resolve())
    for key in ('model', 'expected_version'):
        if agent.get(key) is not None and not isinstance(agent[key], str):
            raise AuditError(ui_message('diagnostic.config.agent_must_be_a_string_or_null', value=key))
    return agent

def last_analysis_settings(value, path):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {'status', 'finished_at', 'results_dir'}:
        raise ValueError(ui_message('diagnostic.config.last_analysis_must_contain_status_finished_at_and'))
    if value['status'] not in ('COMPLETED', 'PARTIAL'):
        raise ValueError(ui_message('diagnostic.config.last_analysis_status_must_be_completed_or_partial'))
    try:
        finished = dt.datetime.fromisoformat(value['finished_at'])
    except (TypeError, ValueError) as exc:
        raise ValueError(ui_message('diagnostic.config.last_analysis_finished_at_must_be_an_iso')) from exc
    if finished.utcoffset() is None:
        raise ValueError(ui_message('diagnostic.config.last_analysis_finished_at_must_include_a_timezone'))
    directory = value['results_dir']
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError(ui_message('diagnostic.config.last_analysis_results_dir_must_be_a_nonempty'))
    directory = Path(directory).expanduser()
    return dict(value, results_dir=str((path.parent / directory).resolve()))


def load_config(path: Path, *, text: str | None = None) -> dict:
    path = path.resolve()
    value = (jsonc if path.suffix.lower() == '.jsonc' else strict_json)(path.read_text() if text is None else text)
    if type(value) is not dict:
        raise AuditError(ui_message('diagnostic.config.configuration_must_be_a_json_object'))
    allowed = {'reports_dir', 'agent', 'stage_agents',
        'output_language', 'priority_scenarios', 'continue_on_error', 'project_description', 'prompts',
        'mode', 'git_mode', 'folder_mode', 'execution', 'result_policy', 'source_decoding',
        'source_filter', 'source_context', 'source_access', 'last_analysis', 'your_editor'}
    if set(value) - allowed:
        raise AuditError(ui_message('diagnostic.config.unknown_configuration_keys', value=set(value) - allowed))
    try:
        value.setdefault('your_editor', 'vscode')
        if value['your_editor'] not in ('vscode', 'zed'):
            raise ValueError(ui_message('diagnostic.config.your_editor_must_be_vscode_or_zed'))
        if 'execution' in value and value['execution'] is None:
            raise ValueError(ui_message('diagnostic.config.execution_must_be_a_json_object'))
        if 'source_context' in value and value['source_context'] is None:
            raise ValueError(ui_message('diagnostic.config.source_context_must_be_a_json_object'))
        if 'source_access' in value and value['source_access'] is None:
            raise ValueError(ui_message('diagnostic.config.source_access_must_be_a_json_object'))
        value['execution'] = execution_settings(value.get('execution'))
        value['source_context'] = source_context_settings(value.get('source_context'))
        value['source_access'] = source_access_settings(value.get('source_access'))
        if (value['source_access']['mode'] == 'mcp' and
                value['source_context'] != {'enabled': True, 'mode': 'on-demand'}):
            raise ValueError(ui_message('diagnostic.config.source_access_mode_mcp_requires_source_context_enabled'))
        value['source_decoding'] = normalize_source_decoding(value.get('source_decoding'))
        value['source_filter'] = normalize_source_filter(value.get('source_filter', {}))
        value['last_analysis'] = last_analysis_settings(value.get('last_analysis'), path)
    except ValueError as exc:
        raise AuditError(str(exc), code='INVALID_CONFIG') from exc
    for key in ('mode', 'reports_dir', 'agent'):
        if key not in value:
            raise AuditError(ui_message('diagnostic.config.missing_configuration_key', value=key))
    mode = value['mode']
    if mode not in ('git', 'folder'):
        raise AuditError(ui_message('diagnostic.config.configuration_requires_mode_git_or_folder'))
    for section in ('git_mode', 'folder_mode'):
        if section in value and type(value[section]) is not dict:
            raise AuditError(ui_message('diagnostic.config.must_be_a_json_object_even_when_inactive', section=section))
    section = mode + '_mode'
    if section not in value:
        raise AuditError(ui_message('diagnostic.config.missing_configuration_section', section=section))
    source = value[section]
    required = ('repository', 'branches', 'baseline_branch') if mode == 'git' else ('path',)
    if set(source) - set(required):
        raise AuditError(ui_message('diagnostic.config.unknown_mode_keys', mode=mode, value=set(source) - set(required)))
    for key in required:
        if key not in source:
            raise AuditError(ui_message('diagnostic.config.missing_configuration_key', value=key))
    source_key = 'repository' if mode == 'git' else 'path'
    for obj, key in ((source, source_key), (value, 'reports_dir')):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise AuditError(ui_message('diagnostic.config.must_be_a_nonempty_path_string', value=key))
        p = Path(obj[key]).expanduser()
        obj[key] = str((path.parent / p if not p.is_absolute() else p).resolve())
    repo, reports = Path(source[source_key]), Path(value['reports_dir'])
    project_reports = (reports / repo.name).resolve()
    if overlap(repo, reports) or overlap(repo, project_reports):
        raise AuditError(ui_message('diagnostic.config.source_directory_and_reports_dir_must_be_disjoint'))
    value['_reports_dir'] = str(project_reports)
    if mode == 'git':
        branches = source['branches']
        if not isinstance(branches, list) or not branches or any(not isinstance(b, str) or not b for b in branches):
            raise AuditError(ui_message('diagnostic.config.at_least_one_nonempty_local_branch_name_is'))
        if len(set(branches)) != len(branches) or source['baseline_branch'] not in branches:
            raise AuditError(ui_message('diagnostic.config.branches_must_be_unique_and_include_baseline_branch'))
    defaults = {'output_language': 'Russian', 'project_description': '', 'priority_scenarios': [],
        'continue_on_error': True, 'stage_agents': {}, 'prompts': {}, 'result_policy': 'compromise'}
    for key, default in defaults.items():
        value.setdefault(key, default)
    try:
        resolve_language(value['output_language'])
    except ValueError as exc:
        raise AuditError(str(exc), code='INVALID_CONFIG') from exc
    if not isinstance(value['project_description'], str):
        raise AuditError(ui_message('diagnostic.config.project_description_must_be_a_string'))
    value['project_description'] = value['project_description'].strip()
    for key in ('agent', 'stage_agents', 'prompts'):
        if type(value[key]) is not dict:
            raise AuditError(ui_message('diagnostic.config.must_be_a_json_object', value=key))
    if type(value['continue_on_error']) is not bool:
        raise AuditError(ui_message('diagnostic.config.continue_on_error_must_be_boolean'))
    if value['result_policy'] not in ('compromise', 'strict'):
        raise AuditError(ui_message('diagnostic.config.result_policy_must_be_compromise_or_strict'))
    if not isinstance(value['priority_scenarios'], list) or any(not isinstance(s, str) for s in value['priority_scenarios']):
        raise AuditError(ui_message('diagnostic.config.priority_scenarios_must_be_an_array_of_strings'))
    stages = STAGES if mode == 'git' and len(source['branches']) > 1 else SOURCE_STAGES
    if not value['execution']['review_enabled']:
        stages = tuple(stage for stage in stages if stage != 'review')
    for section in ('stage_agents', 'prompts'):
        if 'catalog' in value[section]:
            raise AuditError(ui_message(
                'diagnostic.config.remove_catalog_the_catalog_stage_has_been_removed',
                section=section,
            ), code='INVALID_CONFIG')
    backend = value['agent'].get('backend')
    if backend not in BACKENDS:
        raise AuditError(ui_message('diagnostic.config.unsupported_backend', backend=backend))
    for stage in value['stage_agents']:
        if stage not in STAGES:
            raise AuditError(ui_message('diagnostic.config.unknown_stage_override', stage=stage))
        if type(value['stage_agents'][stage]) is not dict:
            raise AuditError(ui_message('tui.stage_agents_must_be_a_json_object', stage=stage))
        backend = value['stage_agents'][stage].get('backend', value['agent']['backend'])
        if backend not in BACKENDS:
            raise AuditError(ui_message('diagnostic.config.unsupported_backend', backend=backend))
    value['_agents'] = {}
    for stage in ('study', *(s for s in stages if s != 'study')):
        agent = dict(value['agent'])
        agent.update(value['stage_agents'].get(stage, {}))
        value['_agents'][stage] = resolve_agent(agent, path)
    value['_prompt_paths'] = {}
    if set(value['prompts']) - (set(STAGES) | {'revise'}):
        raise AuditError(ui_message('diagnostic.config.unknown_prompt_stage'))
    for stage in (*stages, *(('revise',) if value['execution']['review_enabled'] and value['execution']['max_revision_rounds'] else ())):
        prompt_path = value['prompts'].get(stage, str(ROOT / 'prompts' / (stage + '.md')))
        if not isinstance(prompt_path, str) or not prompt_path.strip():
            raise AuditError(ui_message('diagnostic.config.prompts_must_be_a_nonempty_path_string', stage=stage))
        p = Path(prompt_path).expanduser()
        p = (path.parent / p if not p.is_absolute() else p).resolve()
        if inside(p, repo):
            raise AuditError(ui_message('diagnostic.config.prompt_templates_must_be_outside_the_inspected_repository'))
        if not p.is_file():
            raise AuditError(ui_message('diagnostic.config.missing_prompt_template', p=p))
        value['_prompt_paths'][stage] = str(p)
    return value
