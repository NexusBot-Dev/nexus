"""Pre-Deploy-Check für Nexus (nur lesend, ändert nichts).

Aufruf im Projektordner:  python3 tools/check.py
Exit-Code 1, wenn etwas gefunden wurde (praktisch für ein Deploy-Skript).

Prüft:
  1. Syntax aller Python-Dateien
  2. ruff: nicht definierte Namen (F821) und doppelte Definitionen (F811), falls ruff installiert ist
  3. Übersetzungen: gleiche Keys in allen Sprachen, Platzhalter passen zusammen
  4. Keys, die im Code benutzt werden, aber in en.py fehlen
  5. NexusEmojis-Attribute: existieren sie, sind sie nicht leer
  6. Verbotene Muster (Dinge, die uns schon einmal Fehler gebracht haben)
"""
import importlib
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path.cwd()
CODE_DIRS = [d for d in ("cogs", "systems", "database", "dashboard") if (ROOT / d).is_dir()]
CODE_FILES = [f for f in ("bot.py", "config.py", "emojis.py") if (ROOT / f).is_file()]
LANGS = ("en", "de", "es", "pt")

# Keys, die bewusst nur einen Code-Fallback haben (keine Übersetzung nötig)
ALLOWED_MISSING: set[str] = set()

# (Regex, Erklärung). Eine Zeile mit "check:ignore" wird übersprungen.
FORBIDDEN = [
    (r'settings\.get\(\s*["\']color["\']\s*\)', "Farbe immer über db_settings.get_color lesen (Premium-Prüfung)"),
    (r'\.get\(\s*["\'](?:goodbye_enabled|announcements_enabled)["\']\s*,\s*True\s*\)', "Opt-in-Funktion: Default muss False sein"),
    (r'\.format\(\s*error\s*=', "keine rohen Fehlertexte an User ausgeben (zentraler Handler übernimmt das)"),
    (r'\bfetch_channels\(', "GET /guilds/{id}/channels liefert ab 16.11.2026 nur sichtbare Channels"),
    (r'raise ValueError\(f["\'][^"\']*\{e\}', "rohe Discord-Fehlertexte nicht an User weitergeben"),
]

PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
USED_KEY_PATTERNS = [
    re.compile(r'lang\.get\(\s*["\']([a-z0-9_]+)["\']'),
    re.compile(r'["\']((?:help|cmd)_[a-z0-9_]+)["\']'),
]


def py_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for d in CODE_DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    files += [ROOT / f for f in CODE_FILES]
    return files


def check_syntax() -> list[str]:
    problems = []
    for p in py_files():
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            problems.append(f"{p.relative_to(ROOT)}:{e.lineno}: {e.msg}")
    return problems


def check_ruff() -> list[str]:
    exe = shutil.which("ruff")
    if not exe:
        print("· ruff nicht gefunden, übersprungen (Installation: pipx install ruff)")
        return []
    targets = [*CODE_DIRS, *CODE_FILES]
    proc = subprocess.run(
        [exe, "check", "--select", "F821,F811,F601,F841", *targets], capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return []
    return proc.stdout.strip().splitlines()[:40] or [proc.stderr.strip()]


def load_messages(lang: str) -> dict:
    ns: dict = {}
    exec((ROOT / "translations" / f"{lang}.py").read_text(encoding="utf-8"), ns)
    return ns["MESSAGES"]


def load_command_keys(lang: str) -> set[str]:
    path = ROOT / "translations" / f"{lang}_commands.py"
    if not path.is_file():
        return set()
    ns: dict = {}
    exec(path.read_text(encoding="utf-8"), ns)
    keys: set[str] = set()
    for value in ns.values():
        if isinstance(value, dict):
            keys |= {k for k in value if isinstance(k, str)}
    return keys


def check_translations() -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    msgs = {lang: load_messages(lang) for lang in LANGS}
    en = msgs["en"]
    for lang in LANGS[1:]:
        other = msgs[lang]
        for key in sorted(set(en) - set(other)):
            problems.append(f"{lang}: Key fehlt: {key}")
        for key in sorted(set(other) - set(en)):
            problems.append(f"{lang}: Key nur hier, nicht in en: {key}")
        for key in sorted(en.keys() & other.keys()):
            a, b = en[key], other[key]
            if not (isinstance(a, str) and isinstance(b, str)):
                continue
            pa, pb = set(PLACEHOLDER.findall(a)), set(PLACEHOLDER.findall(b))
            dotted = sorted(p for p in pb if "." in p)
            if dotted:
                problems.append(f"{lang}: {key}: Attributzugriff im Platzhalter {dotted}")
            if pb - pa:
                problems.append(f"{lang}: {key}: Platzhalter nur in {lang} (KeyError beim format): {sorted(pb - pa)}")
            if pa - pb:
                warnings.append(f"{lang}: {key}: Platzhalter fehlen: {sorted(pa - pb)}")
    return problems, warnings


def check_used_keys() -> list[str]:
    en = load_messages("en")
    known = set(en) | load_command_keys("en")
    used: dict[str, str] = {}
    for p in py_files():
        text = p.read_text(encoding="utf-8")
        for pattern in USED_KEY_PATTERNS:
            for key in pattern.findall(text):
                used.setdefault(key, str(p.relative_to(ROOT)))
    return [
        f"{key} (z. B. in {where})"
        for key, where in sorted(used.items())
        if key not in known and key not in ALLOWED_MISSING
    ]


def check_emojis() -> list[str]:
    sys.path.insert(0, str(ROOT))
    try:
        emojis = importlib.import_module("emojis")
    except Exception as e:  # noqa: BLE001
        return [f"emojis.py konnte nicht importiert werden: {e!r}"]
    pattern = re.compile(r"NexusEmojis((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
    problems = []
    for p in py_files():
        for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for m in pattern.finditer(line):
                obj = emojis.NexusEmojis
                missing = False
                for part in m.group(1).lstrip(".").split("."):
                    if not hasattr(obj, part):
                        missing = True
                        break
                    obj = getattr(obj, part)
                where = f"{p.relative_to(ROOT)}:{number}"
                if missing:
                    problems.append(f"{where}: NexusEmojis{m.group(1)} existiert nicht")
                elif isinstance(obj, str) and not obj.strip():
                    problems.append(f"{where}: NexusEmojis{m.group(1)} ist leer")
    return problems


def check_patterns() -> list[str]:
    problems = []
    for p in py_files():
        for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "check:ignore" in line:
                continue
            for pattern, message in FORBIDDEN:
                if re.search(pattern, line):
                    problems.append(f"{p.relative_to(ROOT)}:{number}: {message}")
    return problems


def main() -> int:
    trans_problems, trans_warnings = check_translations()
    sections = [
        ("Syntax", check_syntax()),
        ("ruff (undefinierte Namen, doppelte Definitionen)", check_ruff()),
        ("Übersetzungen: Konsistenz zwischen den Sprachen", trans_problems),
        ("Übersetzungen: im Code benutzt, aber nicht in en", check_used_keys()),
        ("NexusEmojis-Attribute", check_emojis()),
        ("Verbotene Muster", check_patterns()),
    ]
    total = 0
    for title, items in sections:
        if items:
            print(f"✗ {title}")
            for item in items:
                print(f"    {item}")
            total += len(items)
        else:
            print(f"✓ {title}")
    if trans_warnings:
        print(f"· {len(trans_warnings)} Hinweise (fehlende Platzhalter, harmlos):")
        for warning in trans_warnings[:15]:
            print(f"    {warning}")
    print(f"\n{'❌' if total else '✅'} {total} Problem(e)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())