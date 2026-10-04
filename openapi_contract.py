#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Bounded structural OpenAPI comparison shared by HTTP profiles and offline diagnostics."""
from collections import Counter
import hashlib
import json
from urllib.parse import unquote

ANNOTATIONS = {'description', 'summary', 'title', 'example', 'examples', 'externalDocs',
               'tags', 'operationId', 'x-codeSamples'}


MAPS = {'paths', 'properties', 'patternProperties', '$defs', 'definitions', 'content', 'responses',
        'headers', 'encoding', 'callbacks', 'links', 'dependentSchemas', 'dependentRequired'}


SETS = {'required', 'enum', 'type', 'allOf', 'anyOf', 'oneOf'}


LITERALS = {'default', 'const', 'enum', 'example', 'examples'}


class DiffError(Exception):
    def __init__(self, kind, message, **details):
        self.kind, self.message, self.details = kind, message, details
        super().__init__(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def pointer(parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def child_role(role, key):
    if role == 'literal':
        return 'literal'
    if role == 'components':
        return 'map'
    if role == 'map':
        return 'node'
    if key in LITERALS:
        return 'literal'
    if key == 'components':
        return 'components'
    return 'map' if key in MAPS else 'node'


def normalize(value, include_docs=False, role='node', depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 150 or count[0] > 1000000:
        raise DiffError('STRUCTURE_LIMIT', 'OpenAPI structure exceeds the comparison limit.')
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if role == 'node' and key in ANNOTATIONS and not include_docs:
                continue
            result[key] = normalize(item, include_docs, child_role(role, key), depth + 1, count)
            if role == 'node' and key in SETS and type(result[key]) is list:
                result[key].sort(key=canonical)
        return result
    if type(value) is list:
        return [normalize(v, include_docs, role, depth + 1, count) for v in value]
    return value


def references(value, at=(), role='node'):
    if role == 'literal':
        return
    if type(value) is dict:
        for key, item in value.items():
            if role == 'node' and key == '$ref':
                yield item, at + (key,)
            elif role != 'node' or key not in ANNOTATIONS:
                yield from references(item, at + (key,), child_role(role, key))
    elif type(value) is list:
        for index, item in enumerate(value):
            yield from references(item, at + (index,), role)


def project(document, selected_paths, side):
    """Keep selected paths and the local-reference closure, without HTTP fetches."""
    output = {'paths': {p: document['paths'][p] for p in selected_paths if p in document['paths']}}
    pending = list(references(output))
    seen = set()
    issues = []
    while pending:
        ref, at = pending.pop()
        if type(ref) is not str:
            issues.append({'side': side, 'at': pointer(at), 'kind': 'invalid_reference'})
            continue
        if ref in seen:
            continue
        seen.add(ref)
        if len(seen) > 10000:
            raise DiffError('STRUCTURE_LIMIT', 'Too many OpenAPI references.')
        fragment = unquote(ref[1:]) if ref.startswith('#') else ''
        if not fragment.startswith('/'):
            issues.append({'side': side, 'at': pointer(at), 'kind': 'external_or_anchor_reference_not_resolved'})
            continue
        parts = [p.replace('~1', '/').replace('~0', '~') for p in fragment[1:].split('/')]
        try:
            value = document
            for part in parts:
                if type(value) is list:
                    if not part.isdecimal() or (len(part) > 1 and part[0] == '0'):
                        raise ValueError()
                    value = value[int(part)]
                else:
                    value = value[part]
        except (KeyError, IndexError, TypeError, ValueError):
            issues.append({'side': side, 'at': pointer(at), 'kind': 'missing_local_reference'})
            continue
        size = 3 if parts[0] == 'components' else 2
        if parts[0] not in ('components', 'paths', '$defs', 'definitions') or len(parts) < size:
            issues.append({'side': side, 'at': pointer(at), 'kind': 'unsupported_local_reference'})
            continue
        # Include the complete referenced component, including its own refs.
        root = parts[:size]
        target, source = output, document
        for part in root[:-1]:
            if type(source) is not dict or type(source.get(part)) is not dict:
                issues.append({'side': side, 'at': pointer(at), 'kind': 'invalid_reference_container'})
                break
            target = target.setdefault(part, {})
            source = source[part]
        else:
            if root[-1] not in target:
                target[root[-1]] = source[root[-1]]
                pending.extend(references(source[root[-1]], tuple(root)))
    return output, issues


def compact(value):
    encoded = canonical(value)
    if len(encoded) <= 240:
        return {'value': value}
    value_type = {dict: 'object', list: 'array', str: 'string', int: 'integer', float: 'number'}[type(value)]
    summary = {'type': value_type, 'length': len(value) if isinstance(value, (dict, list, str)) else len(encoded),
               'sha256': hashlib.sha256(encoded.encode()).hexdigest()}
    if type(value) is dict:
        summary['keys'] = [k if len(k) <= 80 else k[:77] + '...' for k in sorted(value)[:8]]
    return {'summary': summary}


def differences(before, after, at=(), role='node', unordered=False):
    if canonical(before) == canonical(after):
        return
    if type(before) is dict and type(after) is dict:
        for key in sorted(before.keys() | after.keys()):
            path = at + (key,)
            if key not in before:
                yield {'op': 'added', 'path': pointer(path), 'after': compact(after[key])}
            elif key not in after:
                yield {'op': 'removed', 'path': pointer(path), 'before': compact(before[key])}
            else:
                yield from differences(before[key], after[key], path, child_role(role, key),
                                       unordered=role == 'node' and key in SETS)
    elif type(before) is list and type(after) is list:
        if unordered:
            old, new = Counter(map(canonical, before)), Counter(map(canonical, after))
            for op, items in [('array_item_removed', old - new), ('array_item_added', new - old)]:
                for item, count in sorted(items.items()):
                    yield {'op': op, 'path': pointer(at), 'count': count, 'item': compact(json.loads(item))}
        else:
            for index in range(max(len(before), len(after))):
                if index >= len(before):
                    yield {'op': 'added', 'path': pointer(at + (index,)), 'after': compact(after[index])}
                elif index >= len(after):
                    yield {'op': 'removed', 'path': pointer(at + (index,)), 'before': compact(before[index])}
                else:
                    yield from differences(before[index], after[index], at + (index,), role)
    else:
        yield {'op': 'changed', 'path': pointer(at), 'before': compact(before), 'after': compact(after)}


def compare(baseline, candidate, *, include_docs=False, max_changes=200):
    for doc in (baseline, candidate):
        if type(doc) is not dict or type(doc.get('paths')) is not dict:
            raise DiffError('INVALID_OPENAPI', 'Expected an OpenAPI object with a paths object.')
    if type(max_changes) is not int or max_changes < 1:
        raise DiffError('INVALID_LIMIT', 'max_changes must be a positive integer.')
    paths = sorted(baseline['paths'])
    if not paths:
        raise DiffError('INVALID_BASELINE', 'Baseline must declare at least one endpoint to compare.')
    before, base_issues = project(normalize(baseline, include_docs), paths, 'baseline')
    after, candidate_issues = project(normalize(candidate, include_docs), paths, 'candidate')
    changes, total = [], 0
    for change in differences(before, after):
        total += 1
        if len(changes) < max_changes:
            changes.append(change)
    issues = base_issues + candidate_issues
    return {'format': 'opencode-openapi-diff',
        'status': 'INCOMPLETE' if issues else 'DIFFERENT' if total else 'MATCH',
        'scope': {'paths': paths, 'references': 'transitive local closure',
                  'candidate_extra_paths_ignored': len(set(candidate['paths']) - set(paths)),
                  'root_metadata_compared': False, 'documentation_compared': include_docs},
        'total_changes': total, 'included_changes': len(changes), 'truncated': total > len(changes),
        'reference_issues': issues, 'changes': changes,
        'limitations': ['A match covers only the selected declarations, not runtime behavior or native retries.',
            'Component renaming, inline schemas and $ref are not treated as equivalent.',
            'Large changed values are summarized; this is not an executable JSON Patch.',
            'Global security, server addresses, info and unreferenced components are outside this comparison.']}
