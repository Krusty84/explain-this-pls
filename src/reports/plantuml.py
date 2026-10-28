# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Local PlantUML syntax checks and confined replacements of diagram bodies."""
from __future__ import annotations
import copy
import re
import shutil
import sys
import tempfile
from pathlib import Path

from src.contracts.contracts import contract_violation
from src.messages import message as ui_message
from src.reports.document_rendering import (_markdown_tokens, _is_plantuml, _fence_source,
                                             _validate_plantuml)
from src.runtime.artifacts import neutral_temporary_base
from src.runtime.errors import AuditError
from src.runtime.execution import Budget
from src.runtime.process import cli_env, process


VALID_SOURCE = '@startuml\ncomponent A\ncomponent B\nA --> B : data\n@enduml\n'
INVALID_SOURCE = '@startuml\ncomponent "A" aass A\n@enduml\n'


class PlantUML:
    def __init__(self, source_path, reporter):
        self.source_path, self.reporter = source_path, reporter
        self.executable = self.version = None

    def _run(self, arguments, source, budget):
        if self.executable is None:
            local = Path(sys.executable).parent / 'plantuml'
            self.executable = str(local) if local.is_file() else shutil.which('plantuml')
        if self.executable is None:
            raise AuditError(ui_message('diagnostic.plantuml.not_installed'),
                             code='PLANTUML_UNAVAILABLE', failure_layer='compatibility')
        with tempfile.TemporaryDirectory(prefix='archaudit-plantuml-',
                dir=neutral_temporary_base(self.source_path)) as raw:
            cwd = Path(raw)
            env = cli_env(cwd)
            env['PLANTUML_SECURITY_PROFILE'] = 'SANDBOX'
            try:
                return process([self.executable, *arguments], cwd, env, source.encode('utf-8'),
                    reporter=self.reporter, context={'stage': 'plantuml'}, budget=budget)
            except OSError as exc:
                raise AuditError(ui_message('diagnostic.plantuml.process_failed'),
                                 code='PLANTUML_FAILED', failure_layer='execution') from exc

    def check(self, source, budget):
        result = self._run(['-charset', 'UTF-8', '-syntax'], source, budget)
        try:
            output = result['stdout'].decode('utf-8')
            rows = output.strip().splitlines()
            if rows and rows[0] == 'ERROR' and len(rows) >= 3 and int(rows[1]) >= 0:
                return dict(valid=False, line=int(rows[1]) + 1, message='\n'.join(rows[2:]),
                            stdout=output, stderr=result['stderr'].decode('utf-8', errors='replace'))
            if (result['returncode'] == 0 and rows and rows[0] in
                    ('SEQUENCE', 'STATE', 'CLASS', 'OBJECT', 'ACTIVITY', 'USECASE',
                     'COMPONENT', 'DESCRIPTION', 'TIMING', 'COMPOSITE', 'OTHER')):
                return dict(valid=True, line=None, message='', stdout=output,
                            stderr=result['stderr'].decode('utf-8', errors='replace'))
        except (UnicodeError, ValueError):
            pass
        raise AuditError(ui_message('diagnostic.plantuml.process_failed'),
                         code='PLANTUML_FAILED', failure_layer='execution')

    def probe(self, budget=None):
        budget = budget or Budget(30)
        result = self._run(['-version'], '', budget)
        output = result['stdout'].decode('utf-8', errors='replace')
        version = next((line for line in output.splitlines() if line.startswith('PlantUML version ')), None)
        release = re.match(r'PlantUML version (\d+)\.(\d+)\.(\d+)', version or '')
        # -version also checks Graphviz, which is not needed for -syntax.
        if (release is None or tuple(map(int, release.groups())) < (1, 2020, 11)
                or not self.check(VALID_SOURCE, budget)['valid']
                or self.check(INVALID_SOURCE, budget)['valid']):
            raise AuditError(ui_message('diagnostic.plantuml.incompatible'),
                             code='PLANTUML_INCOMPATIBLE', failure_layer='compatibility')
        self.version = version
        return dict(version=version, executable=self.executable)


def diagrams(value):
    """Use CommonMark coordinates; author rules require one standalone fence per block."""
    records = []
    for i, section in enumerate(value['report_sections']):
        for j, block in enumerate(section['blocks']):
            markdown = block['markdown']
            tokens = _markdown_tokens(markdown)
            found = [token for token in tokens if _is_plantuml(token)]
            if not found:
                continue
            location = dict(path=f'$.report_sections[{i}].blocks[{j}].markdown',
                            section_key=section['key'], block_index=j)
            if (len(tokens) != 1 or len(found) != 1
                    or markdown.strip() != _fence_source(markdown, found[0]).strip()):
                raise contract_violation('INVALID_PLANTUML_FENCE', **location)
            token = found[0]
            _validate_plantuml(token, location)
            rows = re.split(r'(?<=\n)|(?<=\r)(?!\n)', markdown)
            records.append(dict(section_key=section['key'], block_index=j, source=token.content,
                section_index=i, path=location['path'],
                start=sum(map(len, rows[:token.map[0] + 1])),
                end=sum(map(len, rows[:token.map[1] - 1]))))
    return records


def validate_replacements(value, context):
    expected = {(item['section_key'], item['block_index']) for item in context['diagrams']}
    received = [(item['section_key'], item['block_index']) for item in value['diagrams']]
    if len(received) != len(expected) or set(received) != expected:
        raise contract_violation('PLANTUML_REPAIR_TARGET_MISMATCH', '$.diagrams')


def replace_diagrams(value, replacements, failed):
    """Only the requested fence bodies are writable; all other values stay intact."""
    validate_replacements(dict(diagrams=replacements), dict(diagrams=failed))
    result = copy.deepcopy(value)
    targets = {(item['section_key'], item['block_index']): item for item in failed}
    for replacement in replacements:
        target = targets[(replacement['section_key'], replacement['block_index'])]
        block = result['report_sections'][target['section_index']]['blocks'][target['block_index']]
        markdown = block['markdown']
        block['markdown'] = (markdown[:target['start']] + replacement['source'].rstrip('\r\n') + '\n'
                             + markdown[target['end']:])
    diagrams(result)
    return result
