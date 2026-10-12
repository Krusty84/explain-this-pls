# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Check published bytes and the separate canonical Review identity."""
import hashlib

from src.reports.document_rendering import validate_published_architecture
from src.reports.presentation import render_architecture


def assert_architecture(test, directory, study, invocation, language):
    published = (directory / 'ARCHITECTURE.md').read_bytes()
    canonical = study['report_markdown'].encode('utf-8')
    validate_published_architecture(published.decode('utf-8'), language)
    test.assertEqual(published, render_architecture(study, language).encode('utf-8'))
    test.assertEqual((directory / 'study.original.md').read_bytes(), canonical)
    test.assertEqual(study['review_plan']['document_sha256'], hashlib.sha256(canonical).hexdigest())
    test.assertEqual(invocation['artifact_hashes']['ARCHITECTURE.md'],
                     {'sha256': hashlib.sha256(published).hexdigest(), 'bytes': len(published)})
    for block in study['block_map']:
        test.assertIn(block['locator']['quote'].encode('utf-8'), published)
    return published.decode('utf-8')
