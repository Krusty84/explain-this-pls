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
from html.parser import HTMLParser

from markdown_it import MarkdownIt
from markdown_it.rules_block import fence

from src.contracts.contracts import (SECTION_KEYS, SECTIONS, CLAIM, MATERIALIZED_CLAIM, SCHEMAS,
    FOLDER_SCHEMAS, LOCATOR, STRINGS, array, obj, string, validate_schema,
    unique_ids, references, contract_violation, document_contract, STUDY_ADVISORY_KEYS)
from src.analysis.evidence import canonical, sha, lines
from src.reports.architecture_format import section_headings

RENDERING_RULE = 'study-blocks-lf'
BLOCK = obj(id=string(), section_key=string(), section_index={'type': 'integer'},
            block_index={'type': 'integer'}, claim_ids=STRINGS, locator=LOCATOR)
PROVENANCE = obj(rule=string(RENDERING_RULE), normalized_sha256=string(),
                 document_sha256=string(), registry_sha256=string(), block_map_sha256=string())


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
    for token in tokens:
        if token.type == 'fence' and not token.meta['closed']:
            raise _markdown_violation('INVALID_MARKDOWN_STRUCTURE', location, 'UNCLOSED_FENCE')
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


def validate_published_architecture(markdown, language):
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


def validate_sections(sections, claims):
    validate_schema(sections, SECTIONS, '$.report_sections')
    validate_schema(claims, array(CLAIM), '$.claims')
    ids = unique_ids(claims, r'C-[0-9]{3,}', '$.claims')
    if [s['key'] for s in sections] != list(SECTION_KEYS):
        raise contract_violation('SECTION_ORDER_MISMATCH', '$.report_sections')
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
    document = lines(value['report_markdown'])
    ids = unique_ids(value['claims'], r'C-[0-9]{3,}', '$.claims')
    for i, block in enumerate(value['block_map']):
        references(block['claim_ids'], ids, f'$.block_map[{i}].claim_ids')
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


def recover_sections(value):
    """Retain only structurally usable authored blocks, with no claimed registry."""
    if type(value) is not list:
        return None
    sections = []
    for section in value:
        if (type(section) is not dict or type(section.get('title')) is not str
                or not section['title'].strip() or any(c in section['title'] for c in '\r\n')
                or type(section.get('blocks')) is not list):
            continue
        blocks = [dict(markdown=b['markdown'], claim_ids=[]) for b in section['blocks']
                  if type(b) is dict and type(b.get('markdown')) is str and b['markdown'].strip()]
        if blocks:
            sections.append(dict(key='', title=section['title'], blocks=blocks))
    return serialize(sections)[0] if sections else None
