# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Fixed headings for the published architecture document."""

SECTION_TITLES = {
    'Russian': (
        'Область исследования', 'Контекст системы', 'Компоненты системы',
        'Запуск и выполнение', 'Данные и состояние',
    ),
    'English': (
        'Study scope', 'System context', 'System components', 'Startup and execution',
        'Data and state',
    ),
}


def resolve_language(language):
    key = language.strip().lower() if isinstance(language, str) else ''
    if key in ('ru', 'russian', 'русский'):
        return 'Russian'
    if key in ('en', 'english', 'английский'):
        return 'English'
    raise ValueError('output_language requires Russian or English architecture headings')


def section_headings(language):
    return tuple(f'{i}. {title}' for i, title in enumerate(SECTION_TITLES[resolve_language(language)], 1))
