"""Importiert jedes Modul einmal — findet z. B. umbenannte Funktionen, die anderswo noch importiert werden.

Aufruf im Projektordner:  python3 tools/check_imports.py
Exit-Code 1 bei Fehlern. Startet keinen Bot und verbindet sich mit nichts.

Braucht die Pakete aus requirements.txt. Fehlende Umgebungsvariablen und Secret-Dateien aus config.py
werden mit Platzhaltern belegt (nur für diesen Prozess) — vorhandene Werte bleiben unangetastet.
"""
import importlib
import os
import pathlib
import re
import sys
import tempfile
import traceback

ROOT = pathlib.Path.cwd()
sys.path.insert(0, str(ROOT))
PACKAGES = ("database", "systems", "dashboard", "integrations", "cogs", "lists")

config_source = (ROOT / "config.py").read_text(encoding="utf-8")
placeholder = pathlib.Path(tempfile.mkdtemp(prefix="nexus-check-")) / "secret"
placeholder.write_text("placeholder")
for name in re.findall(r'_read_secret\(\s*"([A-Z0-9_]+)"', config_source):
    os.environ.setdefault(name, str(placeholder))
for name in re.findall(r'os\.(?:environ\[|environ\.get\(|getenv\()\s*"([A-Z0-9_]+)"', config_source):
    os.environ.setdefault(name, "1")   # IDs, Ports usw.: eine Zahl passt überall

modules = [
    f"{package}.{path.stem}"
    for package in PACKAGES if (ROOT / package).is_dir()
    for path in sorted((ROOT / package).glob("*.py")) if path.stem != "__init__"
]
errors = 0
for module in modules:
    try:
        importlib.import_module(module)
    except Exception:
        errors += 1
        print(f"  ✗ {module}\n{traceback.format_exc(limit=3)}")

print(f"Module importiert: {len(modules)}")
if errors:
    print(f"❌ {errors} Problem(e)")
    sys.exit(1)
print("✅ 0 Problem(e)")
