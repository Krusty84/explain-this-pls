# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Invalid Study responses for required-diagram contract tests."""
from fixtures.ledger_response import response


def missing_diagram(context, section_key):
    data = response(context)
    section = next(section for section in data['report_sections'] if section['key'] == section_key)
    section['blocks'] = section['blocks'][:1]
    return data


def malformed_diagram(context, markdown, section_key='components'):
    data = response(context)
    section = next(section for section in data['report_sections'] if section['key'] == section_key)
    section['blocks'][-1]['markdown'] = markdown
    return data
