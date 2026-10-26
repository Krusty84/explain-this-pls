# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Pure document serialization. No source reads, model calls or publication.

UTF-8, CRLF -> LF only. Preserve spaces, tabs, bare CR and Unicode code points.
Each heading ends in two LF; blocks get a final LF if needed and one separator
LF. A locator includes the block's final LF, never the separator or heading.
"""
from __future__ import annotations
import copy
import json
import re
from html.parser import HTMLParser

from markdown_it import MarkdownIt
from markdown_it.rules_block import fence

from src.contracts.contracts import (SECTION_KEYS, SECTIONS, CLAIM, MATERIALIZED_CLAIM, SCHEMAS,
    FOLDER_SCHEMAS, LOCATOR, STRINGS, array, obj, string, validate_schema,
    unique_ids, references, contract_violation, document_contract, STUDY_ADVISORY_KEYS)
from src.analysis.evidence import canonical, sha, lines
from src.reports.architecture_format import section_headings

RENDERING_RULE = 'study-blocks-lf'
BLOCK = obj(id=string(), section_key=string(*SECTION_KEYS), section_index={'type': 'integer'},
            block_index={'type': 'integer'}, claim_ids=STRINGS, locator=LOCATOR)
PROVENANCE = obj(rule=string(RENDERING_RULE), normalized_sha256=string(),
                 document_sha256=string(), registry_sha256=string(), block_map_sha256=string())
REQUIRED_DIAGRAMS = {'components': 'component_overview', 'data_and_state': 'data_flow'}
SCOPE_GROUPS = {
    'Investigated': 'Исследовано',
    'Partially investigated': 'Частично исследовано',
    'Unverified': 'Не проверено',
    'Out of scope': 'Вне области исследования',
}


def materialized_schema(wire):
    return obj(**{k: v for k, v in wire['properties'].items() if k not in ('report_sections', 'claims')},
               report_markdown=string(), claims=array(MATERIALIZED_CLAIM), block_map=array(BLOCK),
               materialization_provenance=PROVENANCE)


MATERIALIZED_SCHEMAS = {mode: materialized_schema(schemas['study'])
    for mode, schemas in (('git', SCHEMAS), ('folder', FOLDER_SCHEMAS))}


def _checked_fence(state, start_line, end_line, silent):
    """Record closure while CommonMark's container offsets are available."""
    if not fence(state, start_line, end_line, silent):
        return False
    if not silent:
        token = state.tokens[-1]
        last = token.map[1] - 1
        pos = state.bMarks[last] + state.tShift[last]
        end = state.eMarks[last]
        stop = state.skipCharsStr(pos, token.markup[0])
        token.meta['closed'] = (last > start_line and not state.is_code_block(last)
            and state.sCount[last] >= state.blkIndent
            and stop - pos >= len(token.markup) and state.skipSpaces(stop) == end)
    return True


def _markdown_tokens(markdown):
    parser = MarkdownIt('commonmark').enable('table')
    parser.block.ruler.at('fence', _checked_fence,
                          {'alt': ['paragraph', 'reference', 'blockquote', 'list']})
    return parser.parse(markdown)


def _is_plantuml(token):
    info = token.info.split()
    return token.type == 'fence' and bool(info) and info[0].lower() == 'plantuml'


def _fence_source(markdown, token):
    # Match CommonMark's CR/LF line coordinates while retaining the source bytes.
    source_lines = re.split(r'(?<=\n)|(?<=\r)(?!\n)', markdown)
    return ''.join(source_lines[token.map[0]:token.map[1]])


def _plantuml_lines(content):
    """Remove comments and mask quoted literals for local directive checks only."""
    in_comment = False
    for line in content.splitlines():
        active, i = [], 0
        while i < len(line):
            if in_comment:
                end = line.find("'/", i)
                if end < 0:
                    break
                in_comment, i = False, end + 2
            elif line.startswith("/'", i):
                in_comment, i = True, i + 2
            elif line[i] == "'":
                break
            elif line[i] == '"':
                active.append('""')
                i += 1
                while i < len(line):
                    char = line[i]
                    i += 1
                    if char == '\\' and i < len(line):
                        i += 1
                    elif char == '"':
                        break
            else:
                active.append(line[i])
                i += 1
        yield ''.join(active).strip()


def _validate_plantuml(token, location):
    if token.level != 0 or token.info.strip().lower() != 'plantuml' or not token.meta['closed']:
        raise contract_violation('INVALID_PLANTUML_FENCE', **location)
    active = list(_plantuml_lines(token.content))
    starts = [i for i, line in enumerate(active) if re.match(r'(?i)^@startuml(?:\s|$)', line)]
    ends = [i for i, line in enumerate(active) if re.match(r'(?i)^@enduml(?:\s|$)', line)]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise contract_violation('INVALID_PLANTUML_BOUNDARY', **location)
    if any(re.match(r'(?i)^!(?:include\w*|import)\b', line) for line in active):
        raise contract_violation('FORBIDDEN_PLANTUML_DIRECTIVE', **location)
    if not any(active[starts[0] + 1:ends[0]]):
        raise contract_violation('EMPTY_PLANTUML_DIAGRAM', **location)


def _inspect_diagram_records(records, claims):
    registered = {claim['id'] for claim in claims}
    present = set()
    for key, block_index, markdown, claim_ids, path in records:
        if key not in REQUIRED_DIAGRAMS:
            continue
        tokens = _markdown_tokens(markdown)
        diagrams = [token for token in tokens if _is_plantuml(token)]
        if not diagrams:
            continue
        location = dict(path=path, section_key=key, diagram_type=REQUIRED_DIAGRAMS[key],
                        block_index=block_index)
        if (len(tokens) != 1 or len(diagrams) != 1
                or markdown.strip() != _fence_source(markdown, diagrams[0]).strip()):
            raise contract_violation('INVALID_PLANTUML_FENCE', **location)
        _validate_plantuml(diagrams[0], location)
        if not claim_ids or not set(claim_ids) <= registered:
            raise contract_violation('UNLINKED_REQUIRED_DIAGRAM', **location)
        present.add(key)
    return dict(required_sections=list(REQUIRED_DIAGRAMS),
                present_sections=[key for key in REQUIRED_DIAGRAMS if key in present],
                missing_sections=[key for key in REQUIRED_DIAGRAMS if key not in present],
                complete=len(present) == len(REQUIRED_DIAGRAMS), scope='STRUCTURAL_ONLY')


def _require_diagrams(inspection, completion_status, locations):
    if completion_status == 'COMPLETE' and inspection['missing_sections']:
        key = inspection['missing_sections'][0]
        raise contract_violation('REQUIRED_DIAGRAM_MISSING', locations[key],
                                 section_key=key, diagram_type=REQUIRED_DIAGRAMS[key])
    return inspection


def inspect_required_diagrams(sections, claims, *, completion_status=None):
    """Inspect authored blocks without mutation, I/O or architectural inference."""
    records = [(section['key'], j, block['markdown'], block['claim_ids'],
                f'$.report_sections[{i}].blocks[{j}].markdown')
               for i, section in enumerate(sections) for j, block in enumerate(section['blocks'])]
    locations = {key: next((f'$.report_sections[{i}].blocks' for i, section in enumerate(sections)
                           if section['key'] == key), '$.report_sections') for key in REQUIRED_DIAGRAMS}
    return _require_diagrams(_inspect_diagram_records(records, claims), completion_status, locations)


def inspect_materialized_diagrams(value, *, completion_status=None):
    """Read blocks by validated coordinates, including repeated identical blocks."""
    document = lines(value['report_markdown'])
    records = [(block['section_key'], block['block_index'],
                ''.join(document[block['locator']['start_line'] - 1:block['locator']['end_line']]),
                block['claim_ids'], f'$.block_map[{i}].locator')
               for i, block in enumerate(value['block_map'])]
    return _require_diagrams(_inspect_diagram_records(records, value['claims']), completion_status,
                             {key: '$.block_map' for key in REQUIRED_DIAGRAMS})


class _HTMLHeadings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings = []
        self.noncanonical_heading = None

    def handle_starttag(self, tag, attrs):
        if tag in ('h1', 'h2'):
            self.headings.append(tag)
            if self.get_starttag_text() != '<' + tag + '>':
                self.noncanonical_heading = tag


def _markdown_violation(code, location, violation):
    details = dict(location) if isinstance(location, dict) else {'path': location}
    path = details.pop('path', '$.report_sections')
    return contract_violation(code, path, violation=violation, **details)


def _validate_markdown_tokens(tokens, location, *, published=False):
    section_index, diagram_index = -1, 0
    for token in tokens:
        if token.type == 'heading_open' and token.tag == 'h2' and token.level == 0:
            section_index += 1
            diagram_index = 0
        if token.type == 'fence' and not token.meta['closed']:
            if _is_plantuml(token):
                details = dict(location) if isinstance(location, dict) else {'path': location}
                if published and 0 <= section_index < len(SECTION_KEYS):
                    key = SECTION_KEYS[section_index]
                    if key in REQUIRED_DIAGRAMS:
                        details.update(section_key=key, diagram_type=REQUIRED_DIAGRAMS[key],
                                       block_index=diagram_index)
                raise _markdown_violation('INVALID_PLANTUML_FENCE', details, 'UNCLOSED_FENCE')
            raise _markdown_violation('INVALID_MARKDOWN_STRUCTURE', location, 'UNCLOSED_FENCE')
        if _is_plantuml(token):
            diagram_index += 1
        if token.type == 'heading_open' and (token.tag == 'h1' or
                token.tag == 'h2' and (not published or token.level != 0)):
            raise _markdown_violation('UNEXPECTED_TOP_LEVEL_HEADING', location, token.tag.upper())
    # Raw HTML can cross token boundaries. Inspect the complete rendered HTML;
    # escaped code and comments stay literal, including split opening tags.
    parser = MarkdownIt('commonmark').enable('table')
    html = _HTMLHeadings()
    html.feed(parser.renderer.render(tokens, parser.options, {}))
    expected = ['h2' for token in tokens if token.type == 'heading_open' and token.tag == 'h2']
    if html.headings != expected or html.noncanonical_heading:
        heading = 'h1' if 'h1' in html.headings else 'h2'
        raise _markdown_violation('UNEXPECTED_TOP_LEVEL_HEADING', location, 'HTML_' + heading.upper())


def validate_block_markdown(markdown, location):
    """Reject rendered H1/H2 and unterminated fences, including in containers."""
    _validate_markdown_tokens(_markdown_tokens(markdown), location)


def _validate_scope_groups(tokens, location):
    """Check structure only, in Scope fragments or the first report section."""
    sections = [i for i, token in enumerate(tokens)
                if token.type == 'heading_open' and token.tag == 'h2' and token.level == 0]
    if sections:
        tokens = tokens[sections[0] + 3:sections[1] if len(sections) > 1 else len(tokens)]
    aliases = {label.casefold(): group for group, russian in SCOPE_GROUPS.items()
               for label in (group, russian)}
    headings = [i for i, token in enumerate(tokens)
                if token.type == 'heading_open' and token.tag == 'h3' and token.level == 0]
    groups = {}
    for index, start in enumerate(headings):
        group = aliases.get(tokens[start + 1].content.strip().casefold())
        if group is None:
            continue
        if group in groups:
            raise contract_violation('DUPLICATE_SCOPE_GROUP', location, section_key='scope', group=group)
        end = headings[index + 1] if index + 1 < len(headings) else len(tokens)
        groups[group] = tokens[start + 3:end]
    for group in SCOPE_GROUPS:
        if group not in groups:
            raise contract_violation('SCOPE_GROUP_MISSING', location, section_key='scope', group=group)
        body = groups[group]
        # Headings, comments and empty Markdown decoration are not group content.
        content = any(token.content.strip() for i, token in enumerate(body)
            if token.type in ('fence', 'code_block') or token.type == 'inline'
            and (i == 0 or body[i - 1].type != 'heading_open')
            and any(child.type in ('text', 'code_inline', 'image') and child.content.strip()
                    for child in token.children or []))
        if not content:
            raise contract_violation('EMPTY_SCOPE_GROUP', location, section_key='scope', group=group)


def validate_published_architecture(markdown, language, *, canonical=None):
    expected = section_headings(language)
    tokens = _markdown_tokens(markdown)
    _validate_markdown_tokens(tokens, '$.ARCHITECTURE.md', published=True)
    headings = [tokens[i + 1].content for i, token in enumerate(tokens)
                if token.type == 'heading_open' and token.tag == 'h2']
    if len(headings) != len(expected):
        raise contract_violation('REPORT_SECTION_COUNT_MISMATCH', '$.ARCHITECTURE.md',
                                 expected_count=len(expected), actual_count=len(headings))
    if tuple(headings) != expected:
        raise contract_violation('REPORT_HEADING_ORDER_MISMATCH', '$.ARCHITECTURE.md')
    _validate_scope_groups(tokens, '$.ARCHITECTURE.md')
    diagrams = {key: [] for key in REQUIRED_DIAGRAMS}
    section_index = -1
    for token in tokens:
        if token.type == 'heading_open' and token.tag == 'h2':
            section_index += 1
        key = SECTION_KEYS[section_index] if section_index >= 0 else None
        if key in diagrams and _is_plantuml(token):
            _validate_plantuml(token, dict(path='$.ARCHITECTURE.md', section_key=key,
                diagram_type=REQUIRED_DIAGRAMS[key], block_index=len(diagrams[key])))
            diagrams[key].append(_fence_source(markdown, token))
    present = [key for key in diagrams if diagrams[key]]
    inspection = dict(required_sections=list(REQUIRED_DIAGRAMS), present_sections=present,
        missing_sections=[key for key in diagrams if not diagrams[key]],
        complete=len(present) == len(REQUIRED_DIAGRAMS), scope='STRUCTURAL_ONLY')
    if canonical is not None:
        validate_materialized(canonical)
        canonical_diagrams = {key: [] for key in REQUIRED_DIAGRAMS}
        parts = canonical['report_markdown'].split('\n')
        document = [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        for block in canonical['block_map']:
            key = block['section_key']
            if key in canonical_diagrams:
                fragment = ''.join(document[block['locator']['start_line'] - 1:block['locator']['end_line']])
                canonical_diagrams[key].extend(_fence_source(fragment, token)
                    for token in _markdown_tokens(fragment) if _is_plantuml(token))
        if diagrams != canonical_diagrams or inspection != inspect_materialized_diagrams(canonical):
            raise contract_violation('DIAGRAM_PUBLICATION_MISMATCH', '$.ARCHITECTURE.md')
        # Navigation stays outside the exact author fragments, in canonical order.
        from src.reports.navigation import block_marker
        heading_lines = [token.map[0] for token in tokens if token.type == 'heading_open' and token.tag == 'h2']
        published_lines = re.split(r'(?<=\n)|(?<=\r)(?!\n)', markdown)
        for index, start in enumerate(heading_lines):
            end = heading_lines[index + 1] if index + 1 < len(heading_lines) else len(published_lines)
            section = ''.join(published_lines[start:end])
            position = 0
            for block in (b for b in canonical['block_map'] if b['section_index'] == index):
                marker = block_marker(block, canonical)
                fragment = ''.join(document[block['locator']['start_line'] - 1:block['locator']['end_line']])
                found = section.find(marker, position)
                if (markdown.count(marker) != 1 or found < 0
                        or not section.startswith(marker + fragment + '\n', found)):
                    raise contract_violation('AUTHOR_BLOCK_PUBLICATION_MISMATCH', '$.ARCHITECTURE.md')
                position = found + len(marker) + len(fragment)
    return _require_diagrams(inspection, canonical['completion_status'] if canonical else 'COMPLETE',
                             {key: '$.ARCHITECTURE.md' for key in REQUIRED_DIAGRAMS})


def validate_sections(sections, claims):
    validate_schema(sections, SECTIONS, '$.report_sections')
    validate_schema(claims, array(CLAIM), '$.claims')
    ids = unique_ids(claims, r'C-[0-9]{3,}', '$.claims')
    if [s['key'] for s in sections] != list(SECTION_KEYS):
        raise contract_violation('SECTION_ORDER_MISMATCH', '$.report_sections')
    inspect_required_diagrams(sections, claims)
    linked = set()
    for i, section in enumerate(sections):
        if not section['title'].strip() or any(c in section['title'] for c in '\r\n'):
            raise contract_violation('INVALID_SECTION_TITLE', f'$.report_sections[{i}].title')
        if not section['blocks']:
            raise contract_violation('EMPTY_SECTION', f'$.report_sections[{i}].blocks')
        for j, block in enumerate(section['blocks']):
            path = f'$.report_sections[{i}].blocks[{j}]'
            if not block['markdown'].strip():
                raise contract_violation('EMPTY_BLOCK', path + '.markdown')
            validate_block_markdown(block['markdown'], dict(path=path + '.markdown',
                                    section_key=section['key'], block_index=j))
            references(block['claim_ids'], ids, path + '.claim_ids')
            linked.update(block['claim_ids'])
        if section['key'] == 'scope':
            _validate_scope_groups(_markdown_tokens('\n\n'.join(block['markdown'] for block in section['blocks'])),
                                   f'$.report_sections[{i}].blocks')
    for i, claim in enumerate(claims):
        if claim['id'] not in linked:
            raise contract_violation('UNBOUND_CLAIM', f'$.claims[{i}].id')


def serialize(sections):
    """Write coordinates as blocks are emitted; never search finished prose."""
    chunks, blocks, line = [], [], 1
    for i, section in enumerate(sections):
        heading = f"## {i + 1}. {section['title']}\n\n"
        chunks.append(heading)
        line += len(lines(heading))
        for j, block in enumerate(section['blocks']):
            fragment = block['markdown'].replace('\r\n', '\n')
            if not fragment.endswith('\n'):
                fragment += '\n'
            serialized_lines = lines(fragment)
            size = len(serialized_lines)
            blocks.append(dict(id=f'B-{i + 1:03d}-{j + 1:03d}', section_key=section['key'],
                section_index=i, block_index=j, claim_ids=list(block['claim_ids']),
                locator=dict(start_line=line, end_line=line + size - 1, quote=''.join(serialized_lines))))
            chunks.extend((fragment, '\n'))
            line += size + 1
    return ''.join(chunks), blocks


def render_document(sections, claims):
    validate_sections(sections, claims)
    markdown, blocks = serialize(sections)
    registry = copy.deepcopy(claims)
    for claim in registry:
        claim['document_locators'] = [copy.deepcopy(b['locator']) for b in blocks if claim['id'] in b['claim_ids']]
    return markdown, blocks, registry


def materialize_study(wire):
    if type(wire) is not dict:
        validate_schema(wire, SCHEMAS['study'])
    core = document_contract('study', wire)
    validate_schema(core, (FOLDER_SCHEMAS if 'source_directory' in wire else SCHEMAS)['study'])
    markdown, blocks, registry = render_document(wire['report_sections'], wire['claims'])
    result = copy.deepcopy({k: v for k, v in core.items() if k not in ('report_sections', 'claims')})
    observations = {key: wire[key] for key in STUDY_ADVISORY_KEYS if key in wire}
    if observations:
        result['advisory'] = dict(raw_json=json.dumps(observations, ensure_ascii=False), diagnostics=[])
    result.update(report_markdown=markdown, claims=registry, block_map=blocks,
        materialization_provenance=dict(rule=RENDERING_RULE, normalized_sha256=sha(canonical(wire)),
            document_sha256=sha(markdown.encode('utf-8')), registry_sha256=sha(canonical(registry)),
            block_map_sha256=sha(canonical(blocks))))
    validate_materialized(result)
    return result


def validate_materialized(value):
    if type(value) is not dict:
        validate_schema(value, MATERIALIZED_SCHEMAS['git'])
    mode = 'folder' if 'source_directory' in value else 'git'
    schema = MATERIALIZED_SCHEMAS[mode]
    # Saved-only fields are checked by saved_contracts at publication.
    validate_schema({k: v for k, v in value.items()
                     if k not in ('program_checks', 'review_plan', 'normalization_provenance', 'revision_id', 'registry_diff', 'advisory')}, schema)
    section_keys = [block['section_key'] for i, block in enumerate(value['block_map'])
                    if i == 0 or block['section_key'] != value['block_map'][i - 1]['section_key']]
    if len(section_keys) != len(SECTION_KEYS):
        raise contract_violation('REPORT_SECTION_COUNT_MISMATCH', '$.block_map')
    if section_keys != list(SECTION_KEYS):
        raise contract_violation('REPORT_HEADING_ORDER_MISMATCH', '$.block_map')
    block_index = 0
    for i, block in enumerate(value['block_map']):
        if i == 0 or block['section_key'] != value['block_map'][i - 1]['section_key']:
            block_index = 0
        if (block['section_index'] != SECTION_KEYS.index(block['section_key'])
                or block['block_index'] != block_index):
            raise contract_violation('SECTION_ORDER_MISMATCH', f'$.block_map[{i}]')
        block_index += 1
    tokens = _markdown_tokens(value['report_markdown'])
    headings = [token for token in tokens if token.type == 'heading_open' and token.tag == 'h2']
    if len(headings) != len(SECTION_KEYS):
        raise contract_violation('REPORT_SECTION_COUNT_MISMATCH', '$.report_markdown',
                                 expected_count=len(SECTION_KEYS), actual_count=len(headings))
    document = lines(value['report_markdown'])
    ids = unique_ids(value['claims'], r'C-[0-9]{3,}', '$.claims')
    for i, claim in enumerate(value['claims']):
        locators = claim['document_locators']
        expected = [b['locator'] for b in value['block_map'] if claim['id'] in b['claim_ids']]
        if not locators or locators != expected:
            raise contract_violation('DOCUMENT_LINKS_MISMATCH', f'$.claims[{i}].document_locators')
    all_locations = [(b['locator'], f'$.block_map[{i}].locator') for i, b in enumerate(value['block_map'])]
    all_locations += [(loc, f'$.claims[{i}].document_locators[{j}]')
                     for i, c in enumerate(value['claims']) for j, loc in enumerate(c['document_locators'])]
    for loc, path in all_locations:
        a, b = loc['start_line'], loc['end_line']
        if a < 1 or b < a or b > len(document) or not loc['quote'].strip() or ''.join(document[a-1:b]) != loc['quote']:
            raise contract_violation('DOCUMENT_LOCATOR_MISMATCH', path)
    provenance = value['materialization_provenance']
    normalization = value.get('normalization_provenance')
    if normalization is not None:
        from src.contracts.saved_contracts import NORMALIZATION_PROVENANCE
        validate_schema(normalization, NORMALIZATION_PROVENANCE, '$.normalization_provenance')
        if normalization['normalized_sha256'] != provenance['normalized_sha256']:
            raise contract_violation('MATERIALIZATION_HASH_MISMATCH', '$.materialization_provenance.normalized_sha256')
    for key, actual in (('document_sha256', sha(value['report_markdown'].encode('utf-8'))),
                        ('registry_sha256', sha(canonical(value['claims']))),
                        ('block_map_sha256', sha(canonical(value['block_map'])))):
        if provenance[key] != actual:
            raise contract_violation('MATERIALIZATION_HASH_MISMATCH', '$.materialization_provenance.' + key)
    inspection = inspect_materialized_diagrams(value, completion_status=value['completion_status'])
    _validate_scope_groups(tokens, '$.report_markdown')
    _validate_scope_groups(_markdown_tokens('\n\n'.join(block['locator']['quote']
        for block in value['block_map'] if block['section_key'] == 'scope')), '$.block_map')
    for i, block in enumerate(value['block_map']):
        references(block['claim_ids'], ids, f'$.block_map[{i}].claim_ids')
    if value['completion_status'] != 'COMPLETE' and not any(item.strip() for item in value['limitations']):
        raise contract_violation('MISSING_LIMITATIONS', '$.limitations')
    checks = value.get('program_checks')
    if type(checks) is dict and 'diagrams' in checks and checks['diagrams'] != inspection:
        raise contract_violation('DIAGRAM_CHECKS_MISMATCH', '$.program_checks.diagrams')


def recover_sections(value):
    """Retain only structurally usable authored blocks, with no claimed registry."""
    if type(value) is not list:
        return None
    sections = []
    for section in value:
        if (type(section) is not dict or section.get('key') not in SECTION_KEYS
                or type(section.get('title')) is not str
                or not section['title'].strip() or any(c in section['title'] for c in '\r\n')
                or type(section.get('blocks')) is not list):
            continue
        blocks = [dict(markdown=b['markdown'], claim_ids=[]) for b in section['blocks']
                  if type(b) is dict and type(b.get('markdown')) is str and b['markdown'].strip()]
        if blocks:
            sections.append(dict(key='', title=section['title'], blocks=blocks))
    return serialize(sections)[0] if sections else None
