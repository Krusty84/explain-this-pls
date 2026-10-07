# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Byte-based physical lines collected during the existing inventory reads.

Count LF bytes plus one for a nonempty unterminated final line. Blank lines
count, CRLF counts once, and standalone CR is not a separator. No decoding or
language heuristics: these metrics describe analyzed regular files, including
binary files. Empty files have zero lines. Chunk boundaries do not matter.
"""


class PhysicalLines:
    def __init__(self):
        self.newlines = 0
        self.unterminated = False

    def update(self, block):
        if block:
            self.newlines += block.count(b'\n')
            self.unterminated = not block.endswith(b'\n')

    @property
    def count(self):
        return self.newlines + int(self.unterminated)


def physical_lines(data):
    counter = PhysicalLines()
    counter.update(data)
    return counter.count
