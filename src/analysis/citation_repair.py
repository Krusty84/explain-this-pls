# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Conservative local repair of failed source quotes; never change source files."""
import copy
from difflib import unified_diff
from os.path import commonprefix
import re

from src.analysis.evidence import (MAX_FILE_BYTES, _read_confined, _resolve_with_sources,
                                   canonical, resolve_evidence, sha, source_catalog)

RULE = 'LOCAL_CITATION_REPAIR'
LINE_TOLERANCE = 3


def _occurrences(text, quote):
    """Two positions suffice to reject ambiguity, including overlapping matches."""
    result, start = [], 0
    while len(result) < 2:
        found = text.find(quote, start)
        if found < 0:
            break
        result.append(found)
        start = found + 1
    return result


def _indent(text):
    return re.match(r'[ \t]*', text).group()


def _replace_indent(quote, old, new):
    # Keep all newline characters and blank lines; alter one shared prefix only.
    return '\n'.join(part if part in ('', '\r') else new + part[len(old):]
                     for part in quote.split('\n'))


def _match(pointer, source_lines):
    quote = pointer.get('quote')
    if not isinstance(quote, str) or not quote:
        return None
    normalized = quote.replace('\r\n', '\n')
    lower = max(1, pointer['start_line'] - LINE_TOLERANCE)
    upper = min(len(source_lines), pointer['end_line'] + LINE_TOLERANCE)
    window = ''.join(source_lines[lower - 1:upper])
    found = _occurrences(window, normalized)
    if len(found) > 1:
        return None
    repaired_quote, reason = quote, 'NEARBY_EXACT_QUOTE'
    if not found:
        parts = normalized.split('\n')
        nonempty = [part for part in parts if part]
        anchors = [i for i, part in enumerate(parts) if part.strip(' \t')]
        if not anchors:
            return None
        common = commonprefix([_indent(part) for part in nonempty])
        anchor = anchors[0]
        relative = _indent(parts[anchor])[len(common):]
        offset, matches = 0, []
        for index, line in enumerate(source_lines[lower - 1:upper]):
            target = lower - 1 + index + anchor
            if target < upper:
                leading = _indent(source_lines[target])
                if not relative or leading.endswith(relative):
                    prefix = leading[:-len(relative)] if relative else leading
                    if prefix != common:
                        replacement = _replace_indent(quote, common, prefix)
                        if window.startswith(replacement.replace('\r\n', '\n'), offset):
                            matches.append((offset, replacement))
                            if len(matches) > 1:
                                return None
            offset += len(line)
        if not matches:
            return None
        position, repaired_quote = matches[0]
        found, reason = [position], 'UNIFORM_LEADING_INDENT'
    position = found[0]
    matched = repaired_quote.replace('\r\n', '\n')
    first = lower + window[:position].count('\n')
    last = lower + window[:position + len(matched) - 1].count('\n')
    repaired = copy.deepcopy(pointer)
    repaired.update(start_line=min(pointer['start_line'], first), end_line=max(pointer['end_line'], last),
                    quote=repaired_quote)
    return repaired, reason

def _diagnostic(index, pointer, source_lines):
    quote = pointer.get('quote', '')
    if not isinstance(quote, str):
        quote = canonical(quote).decode('utf-8')
    original = ''.join(source_lines[pointer['start_line'] - 1:pointer['end_line']])
    before, after = quote.splitlines(keepends=True), original.splitlines(keepends=True)
    diff = unified_diff(before, after, fromfile='model_quote',
                        tofile=f"{pointer['source_id']}/{pointer['path']}:{pointer['start_line']}-{pointer['end_line']}",
                        n=max(len(before), len(after)))
    # difflib omits the standard marker for unterminated lines. Add it so the
    # final quote line and the first source line cannot run together in the log.
    rendered = ''.join(part if part.endswith(('\n', '\r')) else
                       part + '\n\\ No newline at end of file\n' for part in diff)
    return dict(evidence_index=index, id=pointer.get('id'),
                **{key: pointer[key] for key in ('source_id', 'path', 'start_line', 'end_line')},
                reason='QUOTE_MISMATCH', action='UNRESOLVED', diff=rendered)


def repair_citations(stage, value, context, expected_files=None, *, expected_metadata=None):
    """Return a copy plus a private journal; the caller selects compromise policy.

    Only QUOTE_MISMATCH can change. Each repaired boundary stays within three
    lines of its original value. Exact unique matches precede indent repair.
    Ordinary evidence resolution remains authoritative after every proposal.
    """
    candidate = copy.deepcopy(value)
    journal = dict(rule=RULE, input_sha256=sha(canonical(value)), output_sha256=None, changes=[], diagnostics=[])
    pointers = value.get('evidence', []) if isinstance(value, dict) else []
    if (stage == 'compare' or context.get('prompt_variant') == 'synthesis' or
            not isinstance(pointers, list) or not all(isinstance(p, dict) for p in pointers)):
        journal['output_sha256'] = journal['input_sha256']
        return candidate, journal
    if not pointers:
        journal['output_sha256'] = journal['input_sha256']
        return candidate, journal
    resolutions, sources = _resolve_with_sources(stage, pointers, context, expected_files,
                                                 expected_metadata=expected_metadata)
    catalog = {source['id']: source for source in source_catalog(context)}
    proposals, touched = [], set()
    for index, (pointer, resolution) in enumerate(zip(pointers, resolutions)):
        if resolution['status'] != 'QUOTE_MISMATCH':
            continue
        source = catalog[pointer['source_id']]
        relative = pointer['path'] if source['root'] == '.' else source['root'] + '/' + pointer['path']
        journal['diagnostics'].append(_diagnostic(index, pointer, sources[relative]['lines']))
        match = _match(pointer, sources[relative]['lines'])
        if match:
            repaired, reason = match
            proposals.append((index, repaired, reason))
            touched.add(relative)
    if proposals:
        root = context.get('source_directory', context.get('repository'))

        def assert_sources():
            for relative in sorted(touched):
                _read_confined(root, relative, MAX_FILE_BYTES, expected=sources[relative]['identity'],
                               pinned=expected_metadata, read=False)

        assert_sources()
        # Standalone callers also pin the initial read for the second resolution.
        pins = expected_files if expected_files is not None else {p: s['sha256'] for p, s in sources.items()}
        checked = resolve_evidence(stage, [p for _, p, _ in proposals], context, pins,
                                   expected_metadata=expected_metadata)
        assert_sources()
        for (index, repaired, reason), result in zip(proposals, checked):
            if result['status'] != 'RESOLVED':
                continue
            candidate['evidence'][index] = repaired
            journal['changes'].append(dict(evidence_index=index, id=pointers[index].get('id'),
                original=copy.deepcopy(pointers[index]), repaired=copy.deepcopy(repaired), reason=reason))
    corrected = {change['evidence_index'] for change in journal['changes']}
    for diagnostic in journal['diagnostics']:
        if diagnostic['evidence_index'] in corrected:
            diagnostic['action'] = 'CORRECTED'
    journal['output_sha256'] = sha(canonical(candidate))
    return candidate, journal
