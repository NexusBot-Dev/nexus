from translations.de import MESSAGES as de
from translations.en import MESSAGES as en
from translations.es import MESSAGES as es
from translations.pt import MESSAGES as pt

from translations.de_commands import COMMANDS as de_cmd
from translations.en_commands import COMMANDS as en_cmd
from translations.es_commands import COMMANDS as es_cmd
from translations.pt_commands import COMMANDS as pt_cmd

TRANSLATIONS: dict[str, dict[str, str]] = {
    "de": de,
    "en": en,
    "es": es,
    "pt": pt,
}

COMMAND_LOCALES: dict[str, dict[str, str]] = {
    "de": de_cmd,
    "en": en_cmd,
    "es": es_cmd,
    "pt": pt_cmd,
}

LOCALE_MAP: dict[str, str] = {
    # Deutsch
    "de": "de",

    # Englisch
    "en-US": "en",
    "en-GB": "en",
    "en": "en",

    # Spanisch (Spanien & Lateinamerika)
    "es-ES": "es",
    "es-419": "es",
    "es": "es",

    # Portugiesisch (Brasilien & Portugal)
    "pt-BR": "pt",
    "pt-PT": "pt",
    "pt": "pt",
}