# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Citation warnings preserve authored bytes and expose safe locator facts."""
import copy
import hashlib
import html
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from markdown_it import MarkdownIt

from fixtures.ledger_response import response
from src.analysis.ledger import prepare_result
from src.analysis.evidence import source_catalog
from src.contracts.contracts import ContractError
from src.reports.document_rendering import materialize_study
from src.reports.presentation import render_architecture, render_citation_warnings, render_source_references, render_stage
from src.runtime.reporting import Reporter
from src.tui.state import RunState


def warning(**changes):
    return dict(evidence_id='study:E-001', source_id='source-001', path='app.py', start_line=1, end_line=1,
                reason='QUOTE_MISMATCH', claim_ids=['C-001'], action='UNRESOLVED', **changes)


def presentation_data(wire=None):
    """Valid canonical blocks with synthetic resolver outcomes; no source I/O."""
    context = dict(branch='main', source_commit='a' * 40,
                   submodules=[dict(path='vendor/worker', expected_commit='b' * 40)])
    data = materialize_study(wire or response(context))
    data['program_checks'] = dict(evidence=[dict(id='study:' + entry['id'], status='RESOLVED',
        **{key: entry[key] for key in ('source_id', 'path', 'start_line', 'end_line')})
        for entry in data['evidence']])
    data['review_plan'] = dict(sources=source_catalog(context))
    return data


class CitationPresentationTests(unittest.TestCase):
    def test_architecture_preserves_author_blocks_and_canonical_input(self):
        for text in ('FACT: original claim\n', 'FACT: no final newline', 'FACT: whitespace  \n\n',
                     'bare\rCR', 'trailing\r'):
            wire = response(dict(branch='main', source_commit='a' * 40))
            wire['report_sections'][0]['blocks'][0]['markdown'] = text
            data = presentation_data(wire)
            data['program_checks']['citation_warnings'] = [warning()]
            original = copy.deepcopy(data)
            rendered = render_architecture(data)
            block = text if text.endswith('\n') else text + '\n'
            self.assertIn(block + '\n*Sources:', rendered)
            self.assertEqual(len([line for line in rendered.split('\n') if line.startswith('## ')]), 5)
            self.assertNotIn('## Citation warnings', rendered)
            self.assertEqual(data, original)

    def test_one_claim_one_evidence_and_registry_definition(self):
        rendered = render_architecture(presentation_data())
        self.assertIn('*Sources: <code>app.py:1–1</code> [E-001](#source-e-001)*', rendered)
        self.assertIn('<a id="source-e-001"></a><code>E-001</code>', rendered)
        self.assertIn('RESOLVED; locator resolved; no quote supplied', rendered)

    def test_claim_order_drives_all_unique_references(self):
        wire = response(dict(branch='main', source_commit='a' * 40))
        wire['evidence'] += [wire['evidence'][0] | dict(id='E-002', path='two.py'),
                             wire['evidence'][0] | dict(id='E-003', path='three.py')]
        wire['claims'][0]['evidence_ids'] = ['study:E-002', 'study:E-001']
        wire['claims'].append(wire['claims'][0] | dict(id='C-002', evidence_ids=['study:E-001', 'study:E-003']))
        wire['report_sections'][0]['blocks'][0]['claim_ids'] = ['C-002', 'C-001']
        rendered = render_architecture(presentation_data(wire))
        references = next(line for line in rendered.splitlines() if line.startswith('*Sources:'))
        self.assertEqual(references.count('[E-001]'), 1)
        self.assertLess(references.index('[E-001]'), references.index('[E-003]'))
        self.assertLess(references.index('[E-003]'), references.index('[E-002]'))
        for identifier in ('001', '002', '003'):
            self.assertIn(f'<a id="source-e-{identifier}"></a>', rendered)

    def test_repeated_text_uses_own_block_ids_and_shared_evidence_recurs(self):
        wire = response(dict(branch='main', source_commit='a' * 40))
        wire['evidence'].append(wire['evidence'][0] | dict(id='E-002', path='other.py'))
        wire['claims'].append(wire['claims'][0] | dict(id='C-002', evidence_ids=['study:E-002']))
        wire['report_sections'][0]['blocks'] = [dict(markdown='Repeated.', claim_ids=claims)
            for claims in (['C-001'], ['C-002'], ['C-001'])]
        rendered = render_architecture(presentation_data(wire))
        refs = [line for line in rendered.splitlines() if line.startswith('*Sources:')]
        self.assertEqual(len(refs), 5)
        self.assertIn('[E-001]', refs[0]); self.assertIn('[E-002]', refs[1])
        self.assertEqual(refs[0], refs[2])
        self.assertNotIn('[E-001]', refs[1])

    def test_unbound_introduction_and_unproved_claims_have_no_fake_reference(self):
        wire = response(dict(branch='main', source_commit='a' * 40))
        wire['claims'][0].update(epistemic_kind='HYPOTHESIS', evidence_ids=[])
        wire['claims'].append(wire['claims'][0] | dict(id='C-002', epistemic_kind='UNKNOWN'))
        wire['report_sections'][0]['blocks'] = [dict(markdown='Introduction.', claim_ids=[]),
            dict(markdown='Not verified.', claim_ids=['C-001', 'C-002'])]
        rendered = render_architecture(presentation_data(wire))
        self.assertIn('Introduction.\n\nNot verified.\n\n*Limitation:', rendered)
        self.assertIn('<code>C-001</code> (HYPOTHESIS)', rendered)
        self.assertIn('<code>C-002</code> (UNKNOWN)', rendered)
        self.assertNotIn('*Sources:', rendered)

    def test_bad_locators_have_explicit_statuses(self):
        for status in ('QUOTE_MISMATCH', 'OUT_OF_RANGE', 'NOT_FOUND', 'ACCESS_DENIED', 'UNKNOWN_SOURCE'):
            data = presentation_data()
            data['program_checks']['evidence'][0]['status'] = status
            if status == 'UNKNOWN_SOURCE':
                data['review_plan']['sources'] = []
            rendered = render_architecture(data)
            references = next(line for line in rendered.splitlines() if line.startswith('*Sources:'))
            self.assertIn(status, references)
            self.assertNotIn('RESOLVED', rendered)
            if status == 'QUOTE_MISMATCH': self.assertIn('quote not verified', references)
            if status == 'UNKNOWN_SOURCE': self.assertIn('Source missing', rendered)

    def test_broken_links_fail_instead_of_looking_like_missing_quotes(self):
        for mutation in ('evidence', 'resolution', 'source'):
            data = presentation_data()
            if mutation == 'evidence': data['evidence'] = []
            if mutation == 'resolution': data['program_checks']['evidence'] = []
            if mutation == 'source': data['review_plan']['sources'] = []
            with self.subTest(mutation=mutation), self.assertRaises(ContractError) as caught:
                render_architecture(data)
            self.assertEqual(caught.exception.details['code'], 'UNKNOWN_REFERENCE')

    def test_submodule_path_and_pinned_identity_remain_distinct(self):
        data = presentation_data()
        data['evidence'][0]['source_id'] = 'source-002'
        data['program_checks']['evidence'][0]['source_id'] = 'source-002'
        rendered = render_architecture(data)
        self.assertIn('<code>vendor/worker/app.py:1–1</code>', rendered)
        self.assertIn('<code>source-002</code>', rendered)
        self.assertIn('b' * 40, rendered)
        self.assertIn('&quot;parent&#95;commit&quot;:&quot;' + 'a' * 40, rendered)

    def test_special_path_characters_cannot_create_markdown_or_html(self):
        data = presentation_data()
        path = 'src/👩\u200d💻/a_b*[]()!#~|`<&>/[click](https://evil)/<h2>.py'
        data['evidence'][0]['path'] = path
        rendered = render_architecture(data)
        self.assertNotIn('[click]', rendered)
        self.assertNotIn('<h2>', rendered)
        self.assertIn('&#91;click&#93;&#40;https://evil&#41;', rendered)
        self.assertIn('a&#95;b&#42;&#91;&#93;&#40;&#41;&#33;&#35;&#126;&#124;&#96;&lt;&amp;&gt;', rendered)
        self.assertIn('<code>' + html.escape(path, quote=False) + '</code>', MarkdownIt().render(rendered))

    def test_golden_is_deterministic_pure_and_keeps_canonical_hash_and_blocks(self):
        fixture = Path(__file__).parent / 'fixtures' / 'architecture_presentation.json'
        data = presentation_data(json.loads(fixture.read_text()))
        data['program_checks']['evidence'][2]['status'] = 'QUOTE_MISMATCH'
        original = copy.deepcopy(data)
        canonical_hash = hashlib.sha256(data['report_markdown'].encode()).hexdigest()
        rendered = render_architecture(data)
        golden = fixture.with_suffix('.md').read_bytes()
        self.assertEqual(rendered.encode('utf-8'), golden)
        self.assertEqual(hashlib.sha256(rendered.encode()).digest(), hashlib.sha256(render_architecture(data).encode()).digest())
        self.assertEqual(data, original)
        self.assertEqual(hashlib.sha256(data['report_markdown'].encode()).hexdigest(), canonical_hash)
        for block in data['block_map']:
            self.assertIn(block['locator']['quote'], rendered)
        self.assertIn('### Source registry', rendered)
        self.assertIn('QUOTE_MISMATCH', rendered)

    def test_removing_only_renderer_elements_recovers_exact_canonical_bytes(self):
        wire = json.loads((Path(__file__).parent / 'fixtures' / 'architecture_presentation.json').read_text())
        wire['report_sections'][1]['blocks'][0]['markdown'] = 'Introduction.  \r\n\r\n\t\r\nbare\r'
        data = presentation_data(wire)
        rendered = render_architecture(data)
        published_lines = [part + '\n' for part in rendered.split('\n')[:-1]]
        canonical_lines = [part + '\n' for part in data['report_markdown'].split('\n')[:-1]]
        tokens = MarkdownIt().enable('table').parse(rendered)
        headings = [token.map[0] for token in tokens if token.type == 'heading_open' and token.tag == 'h2']
        first_blocks = [next(block for block in data['block_map'] if block['section_index'] == index)
                        for index in range(5)]
        replacements = {line: canonical_lines[block['locator']['start_line'] - 3]
                        for line, block in zip(headings, first_blocks)}
        indexes = ({claim['id']: claim for claim in data['claims']},
                   {'study:' + entry['id']: entry for entry in data['evidence']},
                   {entry['id']: entry for entry in data['program_checks']['evidence']},
                   {entry['id']: entry for entry in data['review_plan']['sources']})
        inserted = {paragraph for block in data['block_map']
                    for paragraph in render_source_references(block, *indexes).split('\n\n') if paragraph}
        removed = set()
        for token in tokens:
            if token.type == 'paragraph_open':
                start, end = token.map
                if ''.join(published_lines[start:end]).removesuffix('\n') in inserted:
                    self.assertEqual(published_lines[end], '\n')
                    removed.update(range(start, end + 1))
            if token.type == 'heading_open' and token.tag == 'h3' and published_lines[token.map[0]] == '### Source registry\n':
                next_section = next(line for line in headings if line > token.map[0])
                removed.update(range(token.map[0], next_section))
        restored = ''.join(replacements.get(index, line) for index, line in enumerate(published_lines)
                           if index not in removed)
        self.assertEqual(restored.encode('utf-8'), data['report_markdown'].encode('utf-8'))

    def test_parsed_source_links_have_unique_definitions_and_real_statuses(self):
        wire = json.loads((Path(__file__).parent / 'fixtures' / 'architecture_presentation.json').read_text())
        data = presentation_data(wire)
        data['program_checks']['evidence'][2]['status'] = 'QUOTE_MISMATCH'
        for language, title in (('English', 'Source registry'), ('Russian', 'Реестр источников')):
            with self.subTest(language=language):
                rendered = render_architecture(data, language)
                tokens = MarkdownIt().enable('table').parse(rendered)
                headings = [token.map[0] for token in tokens
                            if token.type == 'heading_open' and token.tag == 'h2']
                registries = [token.map[0] for i, token in enumerate(tokens)
                              if token.type == 'heading_open' and token.tag == 'h3'
                              and tokens[i + 1].content == title]
                self.assertEqual(len(registries), 1)
                self.assertLess(headings[0], registries[0])
                self.assertLess(registries[0], headings[1])
                scope = rendered.split('### ' + title, 1)[0]
                for block in data['block_map']:
                    if block['section_key'] == 'scope': self.assertIn(block['locator']['quote'], scope)
                children = [child for token in tokens for child in (token.children or [])]
                destinations = [child.attrGet('href').removeprefix('#') for child in children
                                if child.type == 'link_open' and child.attrGet('href').startswith('#source-e-')]
                definitions = [match.group(1) for child in children if child.type == 'html_inline'
                               and (match := re.fullmatch(r'<a id="(source-e-[0-9]+)">', child.content))]
                self.assertTrue(destinations)
                self.assertEqual(len(definitions), len(set(definitions)))
                self.assertEqual(set(destinations), set(definitions))
                for entry, resolution in zip(data['evidence'], data['program_checks']['evidence']):
                    row = next(line for line in rendered.splitlines()
                               if f'<a id="source-{entry["id"].lower()}"></a>' in line)
                    self.assertIn(resolution['status'], row)

    def test_warning_tables_are_deterministic_escaped_and_do_not_show_quotes(self):
        first = warning()
        first.update(path='unsafe|<path>', claim_ids=['C-002', 'C-001'], quote='PRIVATE SOURCE TEXT')
        second = warning()
        second.update(evidence_id='study:E-002', action='CORRECTED', original_start_line=8, original_end_line=9,
                      branch='feature')
        data = {'program_checks': {'citation_warnings': [second, first]}}
        rendered = render_citation_warnings(data)
        data['program_checks']['citation_warnings'].reverse()
        self.assertEqual(rendered, render_citation_warnings(data))
        self.assertIn('unsafe&#124;&lt;path&gt;', rendered)
        self.assertIn('C-001, C-002', rendered)
        self.assertIn('8-9', rendered)
        self.assertIn('feature / source-001', rendered)
        self.assertNotIn('PRIVATE SOURCE TEXT', rendered)
        self.assertIn('Предупреждения о цитатах', render_citation_warnings(data, 'Russian'))

    def test_annotated_report_marks_impacted_fact_without_promoting_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'app.py').write_text('def main(): return 42\n')
            context = {'source_directory': str(Path(directory).resolve()), 'source_fingerprint': 'pinned',
                       'result_policy': 'compromise'}
            wire = response(context)
            wire['evidence'][0]['quote'] = 'PRIVATE WRONG QUOTE'
            doc = prepare_result('study', wire, context)
            rendered = render_stage('study', doc)
        self.assertIn('Citation checks required: study:E-001 (UNRESOLVED)', rendered)
        self.assertIn('strict policy checks satisfied: False', rendered)
        self.assertIn('| FACT |', rendered)
        self.assertIn('QUOTE_MISMATCH', rendered)
        self.assertNotIn('PRIVATE WRONG QUOTE', rendered)
        self.assertFalse(doc['program_checks']['policy_satisfied'])
        self.assertTrue(doc['program_checks']['continuation_allowed'])

    def test_cli_warning_is_safe_in_verbose_output_and_completion_is_not_failure(self):
        out, err, events = io.StringIO(), io.StringIO(), []
        reporter = Reporter(stdout=out, stderr=err, progress=False, verbose=True, event_sink=events.append)
        reporter.emit('citation_warning', stage='study', branch='unit', **warning(), quote='PRIVATE QUOTE', text='PRIVATE SOURCE')
        reporter.emit('stage_completed', stage='study', status='COMPLETE_WITH_WARNINGS', elapsed_seconds=1)
        reporter.finish({'status': 'COMPLETE_WITH_WARNINGS', 'exit_code': 0, 'manifest': None}, {},
                        check_only=False, config_path=Path('config.json'))
        reporter.close()
        self.assertIn('source-001 / app.py:1-1', err.getvalue())
        self.assertIn('affected claims: C-001', err.getvalue())
        self.assertIn('complete with citation warnings', out.getvalue())
        self.assertNotIn('[FAIL]', out.getvalue() + err.getvalue())
        self.assertNotIn('PRIVATE', out.getvalue() + err.getvalue())
        self.assertNotIn('quote', events[0].context)
        self.assertNotIn('text', events[0].context)

    def test_tui_warning_does_not_fail_stage_and_survives_completion(self):
        state = RunState(clock=lambda: 1)
        context = {'stage': 'study', 'branch': 'unit', 'revision_id': '001'}
        state.apply('stage_started', context)
        state.apply('citation_warning', context | warning())
        row = state.rows[('unit', 'study', '001')]
        self.assertEqual(row['status'], 'RUNNING')
        state.apply('stage_completed', context | {'status': 'COMPLETE_WITH_WARNINGS', 'elapsed_seconds': 1})
        state.finish({'result': {'status': 'COMPLETE_WITH_WARNINGS', 'exit_code': 0}})
        self.assertEqual(row['status'], 'COMPLETE_WITH_WARNINGS')
        self.assertIn('C-001', row['diagnostics'][0]['message'])
        self.assertIn('citation warnings', row['result'])
        self.assertEqual(len(state.diagnostics), 1)


if __name__ == '__main__':
    unittest.main()
