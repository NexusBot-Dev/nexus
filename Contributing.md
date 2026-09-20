# Contributing to Nexus

Thanks for your interest in improving Nexus! 🎉

## Contributing translations

Translations live in `translations/de.py` and `translations/en.py`, registered in `translations/__init__.py`.
Currently supported: German (`de`), English (`en`), with Discord locale variants (`en-US`, `en-GB`) mapped to `en`.

**Adding a new language:**
1. Fork the repo
2. Create a new file, e.g. `translations/fr.py`, following the structure of `de.py`/`en.py` (a `MESSAGES` dict with the same keys)
3. Register it in `translations/__init__.py`:
   ```python
   from translations.fr import MESSAGES as fr

   TRANSLATIONS: dict[str, dict[str, str]] = {
       "de": de,
       "en": en,
       "fr": fr,
   }

   LOCALE_MAP: dict[str, str] = {
       "de":    "de",
       "en-US": "en",
       "en-GB": "en",
       "fr":    "fr",
   }
   ```
4. Translate every key - please don't skip any, or the English fallback will be used
5. Open a pull request with a short note on which language you're adding

**Fixing an existing translation:**
Edit the affected string in the relevant `translations/*.py` file and open a PR. A short reason in the PR description is enough (e.g. "typo in `rr_setup_title`" or "more natural phrasing").

## Before you ask

Check the [Nexus Wiki](https://trynexus.de/en/wiki) first — many questions about commands, modules, and setup are already answered there.

## Reporting bugs & feature requests

Please share these in the [Support Server](https://discord.gg/YFCrvBb6t3) — I usually respond faster there than on GitHub.

## Code contributions

Nexus is a solo project and I maintain the core logic myself. I'm generally 
not looking for pull requests that add features or refactor existing code.

Small, obvious fixes are welcome though — typos, broken links, or clear 
one-line bugs. For anything bigger, please suggest it in the [Support Server](https://discord.gg/YFCrvBb6t3) 
first so we can talk it through before you spend time on it.

## Code of conduct

Be nice. That's really it.
