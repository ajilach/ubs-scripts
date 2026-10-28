"""
The engine's UI strings, and which language to write one in.

`data/translations/<lang>.toml` maps an English master to its wording per language — the table
PROBLEM-default-translations keeps the form dictionaries in step with. This module answers the
question every rule that writes one of those strings has to ask: master or own language?

  form has a dictionary for its own language → the **English master**, because the string is a
  dictionary key and every locale translates it;
  form ships no dictionary at all (52 of 304 packages, 30 of them not English-based) → the wording
  **in the form's own language**, because there the master is not a key, it is the text on screen and
  nothing can change it.
"""
import os
import re

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "translations")
LOCALE_LANG = {"de": "de", "de-ch": "de", "it": "it", "it-ch": "it", "es": "es", "sp": "es",
               "fr": "fr", "en": "en", "en-us": "en", "en-gb": "en"}
_TABLE = {}
_LINE = re.compile(r'^\s*"([^"]+)"\s*=\s*"(.*)"\s*$')


# The Spanish table is filed under the `sp` locale, as the corpus dictionaries are.
FILE_FOR = {"es": "sp"}


def table(lang):
    """{master: wording} for one language, read once."""
    if lang not in _TABLE:
        out = {}
        path = os.path.join(DIR, f"{FILE_FOR.get(lang, lang)}.toml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = _LINE.match(line)
                    if m:
                        out[m.group(1)] = m.group(2)
        _TABLE[lang] = out
    return _TABLE[lang]


def translated(master, lang):
    return table(lang).get(master)


def has_dictionary(locales, lang):
    """True when the package ships a dictionary in the form's own language."""
    return (lang or "en") == "en" or any(LOCALE_LANG.get(loc) == lang for loc in locales or ())


def ui_text(master, lang, locales):
    """The wording to write for `master` on this form."""
    if has_dictionary(locales, lang):
        return master
    return translated(master, lang) or master
