# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Explicit, sequential study experiments using existing frozen assignments."""
import copy
from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path

from src.analysis.analysis_plan import verify_analysis_plan, DEFAULT_MAX_SOURCE_BYTES_PER_SESSION
from src.analysis.evidence import canonical, resolve_evidence, sha, source_catalog
from src.contracts.contracts import strict_json

VARIANTS = (('observe', False, False), ('guard', False, True), ('context', True, True))
FROZEN_FILES = ('source.inventory.json', 'coverage.plan.json', 'analysis.plan.json')


def read_json(path):
    return strict_json(Path(path).read_text(encoding='utf-8'))


def frozen_inputs(baseline, config, shard_id=None):
    inventory, coverage, analysis = [read_json(Path(baseline) / name) for name in FROZEN_FILES]
    verify_analysis_plan(analysis, inventory, coverage,
        max_source_bytes_per_session=config.get('max_source_bytes_per_session', DEFAULT_MAX_SOURCE_BYTES_PER_SESSION))
    source = coverage['sources'][0]['identity']
    if source['mode'] != config['mode']:
        raise ValueError('Baseline source mode differs from the experiment config.')
    context = {key: copy.deepcopy(config[key]) for key in
               ('output_language', 'project_description', 'priority_scenarios')}
    context.update(source_mode=config['mode'], execution_mode='static-only',
        source_access='Read only the pinned source inventory. Do not execute or modify project files. Do not follow symlinks.',
        source_decoding=config.get('source_decoding', {'rules': []}),
        source_decoding_access='Decoding rules apply to program evidence checks only; agent reading depends on its CLI.',
        review_enabled=config['execution']['review_enabled'],
        coverage_plan=coverage, analysis_plan=analysis, _inventory=inventory)
    if source['mode'] == 'folder':
        context.update(source_directory=source['directory'], source_fingerprint=source['fingerprint'])
        expected_path = source['directory']
    else:
        context.update(branch=source['branch'], source_commit=source['commit'],
            repository=config['git_mode']['repository'], submodules=[
                {'path': item['root'], 'expected_commit': item['identity']['commit']}
                for item in coverage['sources'][1:]])
        expected_path = inventory['source_directory']
    configured_path = config[config['mode'] + '_mode']['path' if config['mode'] == 'folder' else 'repository']
    if Path(expected_path).resolve() != Path(configured_path).resolve():
        raise ValueError('Baseline source directory differs from the experiment config.')
    if source_catalog(context) != coverage['sources']:
        raise ValueError('Baseline source identities are inconsistent.')
    if shard_id is None:
        if analysis['required_sessions'] != 1:
            raise ValueError('Select --shard from the frozen plan for a multi-session experiment.')
        context['revision_id'] = '001'
        return 'study', context
    shards = [s for s in analysis['shards'] if s['id'] == shard_id]
    if len(shards) != 1 or not shards[0]['subsystem_ids']:
        raise ValueError('Select a nonempty shard from the frozen plan.')
    context['analysis_shard'] = shards[0]
    return 'study-shard', context


def check_facts(facts, context, metadata):
    if not isinstance(facts, list) or not facts:
        raise ValueError('Preregister at least one fact with source evidence.')
    identifiers = set()
    resolved = []
    for fact in facts:
        if (not isinstance(fact, dict) or not isinstance(fact.get('id'), str) or not fact['id']
                or fact['id'] in identifiers or not isinstance(fact.get('statement'), str)
                or not fact['statement'].strip() or not isinstance(fact.get('evidence'), list)
                or not fact['evidence']):
            raise ValueError('Facts need unique IDs, statements and nonempty evidence lists.')
        identifiers.add(fact['id'])
        checks = resolve_evidence('fact', fact['evidence'], context, expected_metadata=metadata)
        if any(item['status'] != 'RESOLVED' for item in checks):
            raise ValueError('A preregistered fact has unresolved source evidence: ' + fact['id'])
        resolved.append({'id': fact['id'], 'evidence': checks})
    return resolved


def markdown(report):
    lines = ['# Source context comparison', '', 'Scope: one frozen study or shard per attempt.', '',
             'An interrupted attempt measures prevented waste, not a faster successful study.', '',
             '| Variant | Repeat | Outcome | Seconds | Resolved citations | Supported / omitted / unsupported facts | Fact assessment |',
             '|---|---:|---|---:|---|---|---|']
    for row in report['attempts']:
        citation = row.get('evidence_counts', {})
        facts = row.get('fact_assessment', {})
        counts = facts.get('counts')
        lines.append('| {variant} | {repetition} | {outcome} | {duration_seconds} | {citations} | {counts} | {facts} |'.format(
            **row, citations=str(citation.get('RESOLVED', 'unavailable')),
            counts=' / '.join(str(counts.get(k, 0)) for k in ('supported', 'omitted', 'unsupported')) if counts else 'unavailable',
            facts=facts.get('status', 'unavailable')))
    for outcome in ('successful', 'interrupted', 'failed'):
        rows = [r for r in report['attempts'] if r['outcome'] == outcome]
        lines.extend(['', f'{outcome.capitalize()} attempts: {len(rows)}.'])
    lines.extend(['', 'Fact support requires human assessment. Resolved source coordinates do not prove a claim.',
                  'Unknown model identity and incomplete tool telemetry remain limitations.'])
    if report.get('error'):
        lines.extend(['', 'Experiment stopped: ' + report['error']])
    return '\n'.join(lines) + '\n'


def save_report(output, report):
    from explain import atomic, save_json
    save_json(output / 'comparison.json', report)
    atomic(output / 'comparison.md', markdown(report).encode('utf-8'))


def run_comparison(config, baseline, facts, output, *, repetitions=1, shard_id=None, live=False):
    """No model subprocess is launched unless live=True is explicitly supplied."""
    from explain import AuditError, Runner, overlap, repository_lock, save_json
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError('Repetitions must be a positive integer.')
    stage, context = frozen_inputs(baseline, config, shard_id)
    output = Path(output).resolve()
    source_path = Path(config[config['mode'] + '_mode']['path' if config['mode'] == 'folder' else 'repository'])
    if overlap(source_path, output) or overlap(Path(baseline).resolve(), output):
        raise ValueError('Experiment output must be disjoint from sources and the baseline.')
    output.mkdir(parents=True, exist_ok=False)
    config = copy.deepcopy(config)
    report = {'status': 'planned', 'live': live, 'stage': stage, 'shard_id': shard_id,
              'repetitions': repetitions, 'facts': copy.deepcopy(facts), 'attempts': [],
              'invariants': {'config_sha256': sha(canonical(config)),
                  'inventory_sha256': sha(canonical(context['_inventory'])),
                  'coverage_plan_sha256': sha(canonical(context['coverage_plan'])),
                  'analysis_plan_sha256': sha(canonical(context['analysis_plan'])),
                  'facts_sha256': sha(canonical(facts))}}
    save_json(output / 'config.snapshot.json', {k: v for k, v in config.items() if not k.startswith('_')})
    save_json(output / 'facts.json', facts)
    for name, key in zip(FROZEN_FILES, ('_inventory', 'coverage_plan', 'analysis_plan')):
        save_json(output / name, context[key])
    sealed_files = {name: sha((output / name).read_bytes()) for name in
                    (*FROZEN_FILES, 'config.snapshot.json', 'facts.json')}
    report['invariants']['frozen_files'] = sealed_files
    context['_coverage_plan_path'] = str(output / 'coverage.plan.json')
    context['_analysis_plan_path'] = str(output / 'analysis.plan.json')
    save_report(output, report)
    if not live:
        return report
    runner = Runner(config, output)
    baseline_versions, model_actual = None, None
    baseline_config = copy.deepcopy(config)
    templates = {path: sha(Path(path).read_bytes()) for path in config['_prompt_paths'].values()}
    templates[str(Path(__file__).resolve().parents[2] / 'prompts' / 'study-shard.md')] = sha(
        (Path(__file__).resolve().parents[2] / 'prompts' / 'study-shard.md').read_bytes())
    report['invariants']['prompt_files'] = templates
    context_seal = sha(canonical(context))
    report['status'] = 'running'
    runner.manifest = report
    prepared_git = False
    try:
        with repository_lock(source_path) if runner.repo else nullcontext():
            try:
                if runner.repo:
                    pins = runner.repo.preflight([context['branch']])
                    prepared_git = True
                    if pins[context['branch']] != context['source_commit']:
                        raise ValueError('Baseline source commit changed.')
                    runner.repo.checkout(context['source_commit'])
                    if source_catalog(context | {'submodules': runner.repo.submodules(context['source_commit'], verified=True)}) != context['coverage_plan']['sources']:
                        raise ValueError('Baseline submodule identities changed.')
                inventory, metadata = runner.source_inventory()
                if inventory != context['_inventory']:
                    raise ValueError('Baseline source inventory changed.')
                report['fact_source_checks'] = check_facts(facts, context, metadata)
                report['invariants']['cli_versions'] = baseline_versions = runner.check_cli()
                save_report(output, report)
                for repetition in range(1, repetitions + 1):
                    for variant, source_enabled, guard_enabled in VARIANTS:
                        current, _ = runner.source_inventory()
                        if current != inventory or sha(canonical(context)) != context_seal:
                            raise ValueError('Frozen experiment input changed.')
                        runner.assert_coverage_file(context)
                        runner.assert_analysis_file(context)
                        if any(sha((output / name).read_bytes()) != expected for name, expected in sealed_files.items()):
                            raise ValueError('Frozen experiment file changed.')
                        if any(sha(Path(path).read_bytes()) != expected for path, expected in templates.items()):
                            raise ValueError('Prompt template changed during comparison.')
                        if runner.check_cli() != baseline_versions:
                            raise ValueError('CLI version or capabilities changed during comparison.')
                        runner.cfg = copy.deepcopy(baseline_config)
                        runner.cfg['source_context']['enabled'] = source_enabled
                        runner.cfg['execution']['read_loop_guard'] = guard_enabled
                        runner.execution = copy.deepcopy(runner.cfg['execution'])
                        runner.source_context.settings = copy.deepcopy(runner.cfg['source_context'])
                        directory = output / variant / str(repetition)
                        destination = directory / (stage + '.logs')
                        result, meta = None, {}
                        failure = None
                        try:
                            result, meta = runner.invoke(stage, copy.deepcopy(context), destination)
                        except (AuditError, ValueError, OSError) as exc:
                            failure = exc
                            if (destination / 'invocation.json').exists():
                                meta = read_json(destination / 'invocation.json')
                        error = meta.get('error') or {}
                        interrupted = error.get('code') == 'NO_PROGRESS' or error.get('failure_kind') == 'NO_PROGRESS'
                        row = {'variant': variant, 'repetition': repetition,
                            'outcome': 'interrupted' if interrupted else 'successful' if
                                result is not None and result.get('completion_status') == 'COMPLETE' and
                                meta.get('publication_complete') else 'failed',
                            'completion_status': (result or {}).get('completion_status'),
                            'duration_seconds': meta.get('duration_seconds'), 'invocation': meta,
                            'evidence_counts': (result or {}).get('program_checks', {}).get('evidence_counts', {}),
                            'fact_assessment': {'status': 'unavailable', 'reason': 'Human fact assessment has not been supplied.'},
                            'result_path': str(directory / (stage + '.json')) if result is not None else None}
                        report['attempts'].append(row)
                        save_report(output, report)
                        after, _ = runner.source_inventory()
                        if after != inventory:
                            raise ValueError('Source inventory changed during comparison.')
                        if runner.repo:
                            runner.assert_source()
                        actual = meta.get('model_actual')
                        if actual and model_actual is not None and actual != model_actual:
                            raise ValueError('Reported model identity changed during comparison.')
                        if actual:
                            model_actual = actual
                        if failure and (runner.critical_failure or getattr(failure, 'failure_layer', None) in ('integrity', 'cleanup')):
                            raise failure
                report['status'] = 'complete'
            finally:
                if prepared_git:
                    report['restoration'] = runner.repo.restore()
                    if not report['restoration']['restored']:
                        raise ValueError('Git restoration failed.')
    except BaseException as exc:
        report.update(status='failed', error=str(exc) or type(exc).__name__)
        raise
    finally:
        if runner.repo:
            runner.repo.close()
        save_report(output, report)
    return report


def apply_assessments(report, assessments):
    """Apply explicit human judgments; never infer fact support from citations."""
    report = copy.deepcopy(report)
    facts = {fact['id'] for fact in report['facts']}
    rows = {(r['variant'], r['repetition']): r for r in report['attempts']}
    seen = set()
    for assessment in assessments:
        key = (assessment['variant'], assessment['repetition'])
        if key not in rows or key in seen:
            raise ValueError('Assessment attempt is unknown or duplicated.')
        seen.add(key)
        entries = assessment['facts']
        if {e['id'] for e in entries} != facts or len(entries) != len(facts):
            raise ValueError('Assess every preregistered fact exactly once.')
        if any(e['status'] not in ('supported', 'omitted', 'unsupported', 'unassessed') for e in entries):
            raise ValueError('Unknown fact assessment status.')
        rows[key]['fact_assessment'] = {'status': 'partial' if any(e['status'] == 'unassessed' for e in entries) else 'complete',
            'method': 'human', 'facts': entries, 'counts': dict(Counter(e['status'] for e in entries))}
    return report
