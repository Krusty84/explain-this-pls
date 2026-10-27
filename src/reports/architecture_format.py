# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Fixed headings for the published architecture document."""

from src.messages import message as ui_message
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
    raise ValueError(ui_message('diagnostic.architecture_format.output_language_requires_russian_or_english_architecture_headings'))


def section_headings(language):
    return tuple(f'{i}. {title}' for i, title in enumerate(SECTION_TITLES[resolve_language(language)], 1))
