"""Prüft Interaktions-Handler auf die Fehler, die zu "Unknown interaction" (10062) oder falscher Sichtbarkeit führen.

Aufruf im Projektordner:  python3 tools/check_interactions.py
Exit-Code 1, wenn etwas gefunden wurde. Nur lesend.

Discord gibt 3 Sekunden für die erste Antwort auf eine Interaktion. Regeln:
  1. Kein Discord-API-Aufruf (Nachricht senden/holen, Channel anlegen, Reaktion setzen, ...) vor der ersten
     Antwort — vorher defer(). Auch über Hilfsfunktionen, die die Interaktion übergeben bekommen.
  2. Nach einem defer() am Anfang keine zweite Antwort über interaction.response.* — danach nur noch
     followup.send / edit_original_response. Gilt auch für Hilfsfunktionen, die ohne is_done()-Prüfung antworten.
  3. Slash-Command mit defer() ohne ephemeral, später aber ephemeral=True: Die Sichtbarkeit legt das defer()
     fest — die Antwort wäre öffentlich.

Bewusste Ausnahme: "# check:interaction-ok <Grund>" in der def-Zeile des Handlers.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path.cwd()
CODE_DIRS = ("cogs", "systems", "dashboard", "integrations")
IGNORE_MARK = "check:interaction-ok"

RESPONSE_METHODS = {"defer", "send_message", "edit_message", "send_modal", "autocomplete"}
# Hilfsfunktionen, die selbst richtig antworten (prüfen is_done() bzw. bearbeiten die bestehende Antwort)
SAFE_HELPERS = {"show", "send_ephemeral", "report_interaction_error"}
DISCORD_API = re.compile(
    r"\.(create_text_channel|create_category|create_role|create_automod_rule|send|fetch_\w+|add_reaction|"
    r"clear_reaction|remove_reaction|edit|delete|purge|history|add_roles|remove_roles|ban|unban|kick|timeout|"
    r"pin|move_to|set_permissions|create_thread|bans|webhooks|create_webhook|invites|crosspost)\("
)


def interaction_param(fn: ast.AsyncFunctionDef) -> str | None:
    for arg in fn.args.args + fn.args.kwonlyargs:
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        if "Interaction" in annotation or arg.arg == "interaction":
            return arg.arg
    return None


def handler_kind(fn: ast.AsyncFunctionDef, cls: ast.ClassDef | None) -> str | None:
    for dec in fn.decorator_list:
        text = ast.unparse(dec)
        if ".autocomplete(" in text:
            return "autocomplete"
        if "ui.button" in text or "ui.select" in text:
            return "component"
        if "app_commands.command" in text or ".command(" in text or "context_menu" in text:
            return "command"
    if cls is not None and fn.name in ("on_submit", "callback"):
        return "component"
    return None


def call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def passes_interaction(call: ast.Call, name: str) -> bool:
    return any(isinstance(a, ast.Name) and a.id == name for a in call.args + [k.value for k in call.keywords])


def is_response_call(call: ast.Call, iname: str) -> str | None:
    """interaction.response.X(...) -> "X"; interaction.followup.* / edit_original_response -> "followup"."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    base = func.value
    if (func.attr in RESPONSE_METHODS and isinstance(base, ast.Attribute) and base.attr == "response"
            and isinstance(base.value, ast.Name) and base.value.id == iname):
        return func.attr
    if func.attr == "edit_original_response" and isinstance(base, ast.Name) and base.id == iname:
        return "followup"
    if isinstance(base, ast.Attribute) and base.attr == "followup" and isinstance(base.value, ast.Name) and base.value.id == iname:
        return "followup"
    return None


def starts_with_defer(fn: ast.AsyncFunctionDef) -> ast.Call | None:
    first = fn.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Await) and isinstance(first.value.value, ast.Call):
        call = first.value.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "defer" and "response" in ast.unparse(call.func):
            return call
    return None


# ─── Alle async-Funktionen einlesen ──────────────────────────────────────────
functions: dict[str, list[tuple[pathlib.Path, ast.ClassDef | None, ast.AsyncFunctionDef, list[str]]]] = {}
handlers = []
files = [p for d in CODE_DIRS if (ROOT / d).is_dir() for p in sorted((ROOT / d).glob("*.py"))]
for path in files:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    classes = [None, *[n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]]
    for cls in classes:
        for fn in (tree.body if cls is None else cls.body):
            if isinstance(fn, ast.AsyncFunctionDef):
                functions.setdefault(fn.name, []).append((path, cls, fn, lines))
                if interaction_param(fn) and (kind := handler_kind(fn, cls)):
                    handlers.append((path, cls, fn, lines, kind))


def resolve(name: str, path: pathlib.Path):
    """Gleichnamige Funktionen: die aus derselben Datei zuerst (z. B. gibt es _save in mehreren Cogs)."""
    candidates = functions.get(name, [])
    same_file = [c for c in candidates if c[0] == path]
    return same_file or candidates


def all_api_calls(fn: ast.AsyncFunctionDef, path: pathlib.Path, depth=0, seen=frozenset()) -> list[str]:
    """Alle Discord-API-Aufrufe in einer Hilfsfunktion, die die Interaktion nicht bekommt (kann also nicht antworten)."""
    if depth > 4 or id(fn) in seen:
        return []
    seen = seen | {id(fn)}
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            code = ast.unparse(node.value)
            if DISCORD_API.search(code) and not code.startswith("db_"):
                found.append(f"{path.name}:{node.lineno} {code[:80]}")
            name = call_name(node.value)
            if name and not code.startswith("db_") and name in functions:
                for (p2, _c, helper, _l) in resolve(name, path):
                    found.extend(f"über {name}(): {x}" for x in all_api_calls(helper, p2, depth + 1, seen))
    return found


def responds_unguarded(fn: ast.AsyncFunctionDef) -> bool:
    text = ast.unparse(fn)
    return bool(re.search(r"\.response\.(send_message|edit_message|send_modal|defer)\(", text)) and "is_done()" not in text


def api_calls_before_response(fn: ast.AsyncFunctionDef, path: pathlib.Path, depth=0, seen=frozenset()):
    """Discord-API-Aufrufe vor der ersten Antwort (folgt Hilfsfunktionen, die die Interaktion bekommen)."""
    iname = interaction_param(fn)
    if not iname or depth > 4 or id(fn) in seen:
        return [], False
    seen = seen | {id(fn)}
    found, answered = [], False

    class Visitor(ast.NodeVisitor):
        def visit_Await(self, node):
            nonlocal answered
            if answered:
                return
            if isinstance(node.value, ast.Call):
                call = node.value
                name = call_name(call)
                if is_response_call(call, iname) or (name in SAFE_HELPERS and passes_interaction(call, iname)):
                    answered = True
                    return
                code = ast.unparse(call)
                if DISCORD_API.search(code) and not code.startswith("db_"):
                    found.append(f"{path.name}:{node.lineno} {code[:80]}")
                if name in functions and not code.startswith("db_"):
                    for (p2, _c, helper, _l) in resolve(name, path):
                        if passes_interaction(call, iname):
                            sub, _ = api_calls_before_response(helper, p2, depth + 1, seen)
                        else:
                            sub = all_api_calls(helper, p2, depth + 1, seen)
                        found.extend(f"über {name}(): {x}" for x in sub)
            self.generic_visit(node)

        def visit_FunctionDef(self, node):   # verschachtelte Funktionen laufen später
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    for stmt in fn.body:
        Visitor().visit(stmt)
    return found, answered


problems = []
for path, cls, fn, lines, kind in handlers:
    if IGNORE_MARK in lines[fn.lineno - 1]:
        continue
    where = f"{path.relative_to(ROOT)}:{fn.lineno} {(cls.name + '.') if cls else ''}{fn.name}"
    iname = interaction_param(fn)

    # Regel 1
    if kind != "autocomplete":
        found, _ = api_calls_before_response(fn, path)
        for item in dict.fromkeys(found):
            problems.append(f"{where}: Discord-API vor der ersten Antwort — vorher defer(): {item}")

    defer = starts_with_defer(fn)
    if defer is None:
        continue
    # Regel 2
    for node in ast.walk(ast.Module(body=fn.body[1:], type_ignores=[])):
        if isinstance(node, ast.Call):
            kind_of_call = is_response_call(node, iname)
            if kind_of_call and kind_of_call != "followup":
                problems.append(f"{where}:{node.lineno}: interaction.response.{kind_of_call} nach defer() — followup/edit_original_response nutzen")
            name = call_name(node)
            if passes_interaction(node, iname) and name not in SAFE_HELPERS and name in functions:
                if any(responds_unguarded(h) for (_p, _c, h, _l) in resolve(name, path)):
                    problems.append(f"{where}:{node.lineno}: {name}() antwortet selbst ohne is_done()-Prüfung — nach defer() schlägt das fehl")
    # Regel 3
    if kind == "command":
        defer_ephemeral = any(k.arg == "ephemeral" and ast.unparse(k.value) == "True" for k in defer.keywords)
        sends_ephemeral = any(
            isinstance(n, ast.keyword) and n.arg == "ephemeral" and ast.unparse(n.value) == "True"
            for n in ast.walk(ast.Module(body=fn.body[1:], type_ignores=[]))
        )
        if sends_ephemeral and not defer_ephemeral:
            problems.append(f"{where}: defer() ohne ephemeral=True, später aber ephemeral=True — die Antwort wäre öffentlich")

print(f"Interaktions-Handler geprüft: {len(handlers)}")
if problems:
    print()
    for p in problems:
        print(f"  ✗ {p}")
    print(f"\n❌ {len(problems)} Problem(e)")
    sys.exit(1)
print("✅ 0 Problem(e)")
