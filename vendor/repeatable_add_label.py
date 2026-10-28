"""
Shared logic for PROBLEM-repeatable-add-label: a repeatable's Add button reads `Add <panel title>`.

A repeatable is a `guidePanel` carrying `minOccur`/`maxOccur`, and is in scope only if it has an Add
button of its own. The label is always the panel's `jcr:title`, phrased per language by `PATTERNS`
(`Add Client`, `Kunde hinzufügen`), or the bare verb from `ADD_ONLY` when the panel has no title,
and it is always written. The wording is decided on the panel by PROBLEM-repeating-panel; this rule
only carries it to the button, so the two cannot disagree and a person only ever writes the title.
An English-based form also gets the dictionary entries for the other locales it has.

This module holds the package plumbing (`parse`, `walk`, `tag_span`, `form_entry_and_xml`,
`locales`, `plain`, `draw_text`, …), the fragment-title table, the wording table and its
translations, base-language detection, Add-button discovery and `classify`/`classify_all`. It is
imported by `find_repeatable_add_label.py` (report), `set_repeatable_add_label.py` (fix) and
`repeating_panel.py`, which derives the panel title and keeps the helpers for doing so.
"""
import html
import json
import os
import re
import zipfile

from xml.parsers import expat

# The styling PROBLEM-repeating-panel puts on a panel it has normalised.
NUMBERING_STYLE = "Repeating Panel Numbering"


def is_numbered_panel(n):
    return n.attrs.get("dorFieldStyling") == NUMBERING_STYLE

MIN_VOTE_MARGIN = 0           # a base language must win by more than this, else English is used
MAX_WORDS = 4
MAX_CHARS = 42
STOP_WORDS = {"name", "nome", "nombre", "no", "nr", "number", "details", "data"}

# ── the wording table ───────────────────────────────────────────────────────────
# The wording is data, so it lives in `data/concepts.json`: a wording change is then not a code
# change, and another sweep needing the same words does not repeat them. `PATTERNS` maps a language
# to how its Add button phrases a label, `ADD_ONLY` to the bare verb for a panel with no title, and
# `CONCEPTS` maps a concept to, per language, the `noun` a rule writes and the `variants` that
# identify the concept in existing text.
def _load_concepts(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "concepts.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    concepts = {c: {l: (v["noun"], v["variants"]) for l, v in langs.items()}
                for c, langs in data["concepts"].items()}
    return data["patterns"], data["add_only"], concepts


PATTERNS, ADD_ONLY, CONCEPTS = _load_concepts()

I18N_LOCALES = ("de", "it", "es")     # what a multilingual English-based form gets translations for


# {fragRef: the fragment's own first drawn title}, for repeatables whose only content is a fragment.
# Vendored from the UBS fragment packages to `data/fragment_titles.json` so the sweep is
# self-contained; regenerate it with `data/extract_fragment_titles.py` over a fresh UBS export.
FRAGMENT_TITLES = {}


def fragment_title(ref, lang="en"):
    """A fragment's first drawn title in one language, falling back to the English master."""
    entry = FRAGMENT_TITLES.get((ref or "").rstrip("/"))
    if not entry:
        return None
    return entry.get(lang) or entry.get("en")


def fragment_translations(ref):
    """{language: title} for a fragment, or {}."""
    return FRAGMENT_TITLES.get((ref or "").rstrip("/")) or {}


# ── package plumbing ────────────────────────────────────────────────────────────
def form_entry_and_xml(zip_path):
    """(entry, xml) of the form's cq:Page .content.xml, or (None, None)."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.endswith("/.content.xml") and "/forms/af/" in n:
                    t = z.read(n).decode("utf-8", "replace")
                    if '"cq:Page"' in t:
                        return n, t
    except (zipfile.BadZipFile, OSError):
        return None, None
    return None, None


def locales(zip_path):
    """The form's dictionary locales, ignoring the sc/tc pseudo-locales."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return sorted({n.rsplit("/", 1)[1][:-4] for n in z.namelist()
                           if "/dictionary/" in n and n.endswith(".xml")}
                          - {"sc", "tc"})
    except (zipfile.BadZipFile, OSError):
        return []


LOCALE_LANG = {"de": "de", "de-ch": "de", "it": "it", "it-ch": "it", "es": "es", "sp": "es",
               "en": "en", "en-us": "en", "en-gb": "en"}


def form_languages(form_locales):
    """The languages a form has dictionaries for, collapsing regional variants. These are the
    translations, not the base: a form with only a `de` dictionary can still be authored in English."""
    return {LOCALE_LANG[l] for l in form_locales if l in LOCALE_LANG}


# The metadata control the summary panel carries. PROBLEM-metadata-languages derives its master
# language from the languages the form was ISSUED in (its source PDFs) and the form's own text, and
# that is the form's own statement of what language it is written in — better evidence than any
# reading of the text, and the one the whole package is built around.
METADATA_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/metadata"
MASTER_ATTR = "formrange_afmasterlanguage"
MASTER_LANG = {"EN": "en", "DE": "de", "IT": "it", "SP": "es", "ES": "es", "FR": "fr"}


def declared_language(root):
    """The master language the form's own metadata control declares, or None.

    `SP` is the metadata's spelling of Spanish; a code this repo has no wording for (`FR`) is
    returned as-is by `metadata_languages` and read as nothing here, which falls back to the vote."""
    for n in walk(root):
        if METADATA_RT not in (n.attrs.get("sling:resourceType") or ""):
            continue
        code = (n.attrs.get(MASTER_ATTR) or "").strip().upper()
        if code:
            return MASTER_LANG.get(code)
    return None


def voted_language(root):
    """The language the form's own inline text votes for — the fallback evidence.

    Kept as the reading of the text itself, which is what PROBLEM-metadata-languages derives the
    declared master from; `base_language` prefers that declaration.

    Original note: the language the form's own inline text is written in — its base.

    Voted by majority over every drawn title that matches a dictionary variant, because the form's
    own text is the only evidence of the base: the list of dictionaries names the translations, so a
    form whose titles read "Client" / "Banking relationship" is English-based even when its only
    dictionary is German.

    A title whose winning variant belongs to more than one language (`Firma` reads the same in
    Italian and Spanish) counts only for whichever of those the form's other titles already vote
    for. A language nobody contests wins, however few titles it has. Two within MIN_VOTE_MARGIN of
    each other, or nothing but shared words to go on, leaves the base undecided and English is used.
    None when nothing matched at all."""
    votes, shared = {}, []
    for n in walk(root):
        # A normalised repeating panel's title counts as form text alongside the draws: it holds
        # the words the draws it replaced used to carry, and without those votes an Italian form
        # reads as English.
        t = normalise_subject(draw_text(n) or (plain(n.attrs.get("jcr:title"))
                                               if is_numbered_panel(n) else None))
        if not t or not is_sane(t):
            continue
        _, langs = best_match(t)
        if len(langs) == 1:
            votes[langs[0]] = votes.get(langs[0], 0) + 1
        elif langs:
            shared.append(langs)
    # A shared word counts for the language the rest of the form already speaks: `Firma` is Italian
    # in a form that also says `Relazione bancaria`, and evidence of nothing in one that does not.
    for langs in shared:
        backed = [l for l in langs if l in votes]
        if len(backed) == 1:
            votes[backed[0]] += 1
    if not votes:
        return "en" if shared else None
    ranked = sorted(votes.values(), reverse=True)
    if len(ranked) > 1 and ranked[0] - ranked[1] <= MIN_VOTE_MARGIN:
        return "en"
    return max(votes, key=votes.get)


def base_language(root, declared=True):
    """The language the form is written in — what it declares, else what its text votes.

    **The metadata wins (owner directive 2026-09-09).** The vote below only ever sees the titles that
    match a wording-table concept, which on a short form is two or three words: AAFA_019, written
    entirely in German, matched `Finanzdienstleister` (DE) and the heading `Signature(s)` (EN), tied
    1-1, and a tie reads as English — so its Add button was labelled `Add` and its titles would have
    been written as English masters. The metadata control states the answer outright, derived by
    PROBLEM-metadata-languages from the languages the form was issued in.

    `declared=False` is for that rule itself: it derives the master partly FROM this reading, so
    letting it read the metadata back would make whatever is written there confirm itself forever.
    """
    return (declared_language(root) if declared else None) or voted_language(root)


def load_fragment_titles(path=None):
    """Load {fragRef: {language: first drawn title}}; defaults to the data file next to this module.

    Every entry carries the master string under `en` plus whatever the fragment package's own
    dictionaries spell it as. The master is what a rule reasons about and what goes into a
    `jcr:title`, because it is the key the translations hang off."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data", "fragment_titles.json")
    try:
        with open(path, encoding="utf-8") as f:
            FRAGMENT_TITLES.update(json.load(f))
    except OSError:
        pass
    return FRAGMENT_TITLES


# ── tree ────────────────────────────────────────────────────────────────────────
class N:
    __slots__ = ("tag", "attrs", "children", "parent", "start")

    def __init__(s, tag, attrs, parent, start):
        s.tag, s.attrs, s.children, s.parent, s.start = tag, attrs, [], parent, start


def parse(xml):
    root = N("__root__", {}, None, -1)
    stack = [root]
    p = expat.ParserCreate()

    def start(name, a):
        node = N(name, a, stack[-1], p.CurrentByteIndex)
        stack[-1].children.append(node)
        stack.append(node)

    p.StartElementHandler = start
    p.EndElementHandler = lambda name: stack.pop()
    p.Parse(xml.encode("utf-8"), True)
    return root


def tag_span(data, start):
    """(start, end) of the open tag, quote-aware — rich-text values contain a literal '>'."""
    i, in_q = start, False
    while i < len(data):
        ch = data[i:i + 1]
        if ch == b'"':
            in_q = not in_q
        elif ch == b">" and not in_q:
            return start, i + 1
        i += 1
    raise ValueError(f"unterminated open tag at byte {start}")


def walk(n):
    yield n
    for c in n.children:
        yield from walk(c)


def panel_children(p):
    """The content children of a panel (through its `items` node)."""
    for c in p.children:
        if c.tag == "items":
            return [k for k in c.children if k.tag not in ("layout", "cq:responsive")]
    return []


def owning_panel(n):
    p = n.parent
    while p is not None and p.tag == "items":
        p = p.parent
    return p




def plain(v):
    return " ".join(re.sub(r"</?[^>]+>", " ", html.unescape(v or "")).split())


def is_draw(n):
    rt = n.attrs.get("sling:resourceType", "")
    return n.attrs.get("guideNodeClass") == "guideTextDraw" or "draw" in rt


MARKER = re.compile(r"<sup\b.*?</sup>", re.S | re.I)


FOOTNOTE = re.compile(r'<(p|div|span)\b[^>]*class="[^"]*footnote[^"]*"[^>]*>.*?</\1>', re.S | re.I)

BLOCK = re.compile(r"</p>|<br\s*/?>|</div>|</h[1-6]>", re.I)


def drawn_text(value):
    """The text a draw shows, first block only.

    A title draw carries its footnotes with it: a `<sup>` marker in the text and a hidden
    `footnoteDescription` paragraph after it. Neither is part of the title, and together they run to
    many times its length."""
    raw = FOOTNOTE.sub(" ", html.unescape(value or ""))
    raw = MARKER.sub(" ", raw)
    for block in BLOCK.split(raw):
        t = plain(block)
        if t:
            return t
    return None


def draw_text(n):
    return drawn_text(n.attrs.get("_value")) if is_draw(n) else None










def is_repeatable(n):
    return (n.attrs.get("guideNodeClass") == "guidePanel"
            and ("maxOccur" in n.attrs or "minOccur" in n.attrs))


def is_add_button(node, scripts, rep_name):
    """Does this button add an instance of `rep_name`? Remove buttons drive the same manager, so the
    script must call `addInstance` or the button must be named `BT_Add*`.

    The panel is named either directly (`RCP_X.instanceManager`) or through its wrapper
    (`this.parent.RCP_X.instanceManager`, the canonical shape) — the second contains the first as a
    substring, so one test covers both. Neither covers a bare `addInstance` call with no manager in
    sight, which is why ownership is read from `primary_rep` and not from here."""
    if node.attrs.get("guideNodeClass") != "guideButton":
        return False
    if rep_name and f"{rep_name}.instanceManager" not in scripts \
            and primary_rep(scripts) != rep_name:
        return False
    return "addInstance" in scripts or (node.attrs.get("name") or "").lower().startswith(("bt_add", "add"))


MAX_BUTTON_CLIMB = 3          # how many ancestor levels to widen the Add-button search by


def root_of(n):
    while n.parent is not None:
        n = n.parent
    return n


_REP_NAMES = {}


def rep_names(root):
    """The names of the repeatables this form actually has, cached per tree."""
    if id(root) not in _REP_NAMES:
        _REP_NAMES[id(root)] = {n.attrs.get("name") for n in repeatables(root)}
    return _REP_NAMES[id(root)]


def add_buttons_for(rep, buttons):
    """Every Add button belonging to this repeatable, by ownership, else scope then proximity.

    Neither signal is enough alone. Name and script link do not identify a button, because a form
    reuses the name `BT_Add` for every repeatable and two repeatables can share a name, so one
    button's script names both. Nearest-in-document alone is worse: a repeatable with no Add button
    of its own latches onto a neighbour's.

    So: the buttons whose script adds an instance of this repeatable first are its own, wherever they
    sit and however many they are — a configurator repeats the same panel with an Add button per
    variant, and they all name the same thing. Failing that, start at the repeatable's own container
    and climb at most `MAX_BUTTON_CLIMB` ancestors, stopping at the first level that holds a
    candidate, and take the nearest one there. A button another repeatable owns is never taken: it
    adds a row here as a side effect and is labelled from the panel it was placed with. A repeatable
    left with no button of its own is out of scope. "Another repeatable" means one this form has: a
    button inside a fragment names the fragment's own panel.
    """
    rep_name = rep.attrs.get("name") or ""
    if not rep_name:
        return []
    scripts = {id(b): sc for b, sc in buttons}
    owned = [b for b, sc in buttons if primary_rep(sc) == rep_name]
    if owned:
        return owned
    scope = owning_panel(rep)
    for _ in range(MAX_BUTTON_CLIMB):
        if scope is None:
            return []
        found = [n for n in walk(scope)
                 if n is not rep and is_add_button(n, scripts.get(id(n), ""), rep_name)]
        if found:
            best = min(found, key=lambda b: abs(b.start - rep.start))
            owner = primary_rep(scripts.get(id(best), ""))
            # Only a name this form actually repeats can own a button away from this repeatable: the
            # script of a button inside a fragment names the fragment's own panel, which is not here.
            taken = owner and owner != rep_name and owner in rep_names(root_of(rep))
            return [] if taken else [best]
        scope = owning_panel(scope)
    return []


def add_button_for(rep, buttons):
    """The one Add button that stands for this repeatable — the nearest of the buttons it owns."""
    owned = add_buttons_for(rep, buttons)
    return min(owned, key=lambda b: abs(b.start - rep.start)) if owned else None


def collect_buttons(root):
    """[(button, its scripts as one string), …]. `\n` and friends are literal in these blobs and
    would glue themselves to the name that follows (`\nPN_X` reads as `nPN_X`), so they go first."""
    out = []
    for n in walk(root):
        if n.attrs.get("guideNodeClass") == "guideButton":
            scripts = "".join(" " + re.sub(r"\\[nrt]", " ", html.unescape(v))
                              for c in walk(n) for k, v in c.attrs.items() if k.startswith("fd:"))
            out.append((n, scripts))
    return out


# The two ways a button says which panel it adds to. The first is what the converter emitted and what
# every unswept form still carries. The second is the canonical rule PROBLEM-repeating-panel writes —
# `window.forms.ubs.addInstance(this.parent.RCP_Client)` — which names the panel through its wrapper,
# because panel names are not unique. Both are matched so this rule reads a swept form and an unswept
# one the same way, and neither sweep has to run before the other.
# The `this.parent.` prefix is required, not optional: forms already call
# `window.forms.ubs.addInstance(RCP_Product)` with a bare name, and matching those would change how
# this rule reads forms nobody has swept. Only the wrapper-qualified spelling is new.
ADD_CALLS = (re.compile(r"(\w+)\.instanceManager\.addInstance"),
             re.compile(r"window\.forms\.ubs\.addInstance\(\s*this\.parent\.(\w+)"))


def primary_rep(scripts):
    """The name of the repeatable an Add button is for, or None.

    One button can append a row to a data panel and to its signature twin in the same script; the
    panel it is placed with comes first. The others are appended as a side effect and must not have a
    say in the label, or the two would fight over the same button. So the leftmost call wins,
    whichever of the two spellings it is written in."""
    hits = [(m.start(), m.group(1)) for p in ADD_CALLS for m in [p.search(scripts)] if m]
    return min(hits)[1] if hits else None




def capitalise(text):
    return (text or "")[:1].upper() + (text or "")[1:]


def subject_translations(master):
    """{language: wording} for a title written as its English master, or {}.

    Two sources, both data files rather than tables in code: a fragment's own dictionaries
    (`fragment_titles.json`) and the concept table (`concepts.json`). What comes back goes into the
    form's dictionary beside the master, and the master itself is the key — which is why a title is
    always written in English even on an Italian form."""
    wanted = normalise_subject(master) or master

    # Concept table first, the fragment's own wording layered over it: a fragment named `Account
    # holder` whose package ships no translations would otherwise shadow the concept table that has
    # them, and the Italian form would get no entry at all.
    out = {}
    # By the concept's own English noun first, then by the variants. A master is written from the noun,
    # and three concepts do not list their noun among their English variants — matched by variant only,
    # those came back with no translations at all and the form was left reading the English master.
    concept = next((c for c, langs in CONCEPTS.items()
                    if "en" in langs
                    and (normalise_subject(langs["en"][0]) or "").casefold() == wanted.casefold()),
                   None)
    if not concept:
        concept, _lang = match_concept(wanted)
    if concept:
        out.update({lang: capitalise(v[0]) for lang, v in CONCEPTS[concept].items()})
    for entry in FRAGMENT_TITLES.values():
        if normalise_subject(entry.get("en") or "") == wanted:
            out.update({lang: capitalise(text) for lang, text in entry.items() if text})
            break
    return out


def normalise_subject(t):
    """Strip numbering and punctuation: `Account #`, `Account 1`, `<b>Account </b>` → `Account`."""
    if not t:
        return None
    t = re.sub(r"&nbsp;?", " ", t)
    # `cliente/i`, `del/i`, `seguente/i`: a slashed one- or two-letter tail is a plural marker, not
    # a word of its own, and counting it as a word pushes short headings past the prose limit.
    t = re.sub(r"(?<=\w)/\w{1,2}\b", "", t)
    t = re.sub(r"[\d#*:()\[\]./,]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—§") or None


def is_sane(t):
    return bool(t) and t.lower() not in STOP_WORDS and len(t.split()) <= MAX_WORDS and len(t) <= MAX_CHARS


def best_match(subject):
    """(concept, [language, …]) for a piece of text, or (None, []).

    A variant only counts at the start of a word — German compounds it into longer ones
    (`Kundendaten` is a `kunde`), but `Abgeltungsteuerbefreite` is not an `Erbe`. Among the variants
    that match, the leftmost wins: a heading names what it is about first and qualifies it
    afterwards, so `Depotinhaber (nachstehend Kunde)` is about the Depotinhaber. At the same position the longest
    wins, being the more specific reading — `Kontoinhaber` is an account holder and not an account,
    and `Cliente` is the Italian word, not the English `client` inside it.

    Several languages can share the winning variant (`cliente` is both Italian and Spanish); all of
    them are returned, in declaration order, so the caller can tell a language apart from a guess."""
    low = subject.lower()
    best, langs_at_best = None, []
    for concept, langs in CONCEPTS.items():
        for lang, (_, variants) in langs.items():
            for v in variants:
                m = re.search(rf"(?<![^\W\d_]){re.escape(v)}", low)
                if not m:
                    continue
                key = (m.start(), -len(v))
                if best is None or key < best[0]:
                    best, langs_at_best = (key, concept), [lang]
                elif key == best[0] and best[1] == concept and lang not in langs_at_best:
                    langs_at_best.append(lang)
    return (best[1], langs_at_best) if best else (None, [])


def match_concept(subject):
    """(concept, language) for a piece of text, or (None, None) — `best_match`, first language."""
    concept, langs = best_match(subject)
    return (concept, langs[0]) if concept else (None, None)








def phrase(pattern, title):
    """`title` in `pattern`, lower-cased when the verb comes first.

    `Add portfolio`, but `Portfolio hinzufügen` — German opens with the noun, which is capitalised
    there. A first word in capitals is an acronym (`ISIN`, `IBAN`) and is left alone."""
    first = title.split(" ", 1)[0]
    if not pattern.startswith("{}") and not first.isupper():
        title = title[:1].lower() + title[1:]
    return pattern.format(title)


def subject_from_label(label, lang):
    """The subject inside an Add label written in `lang`, or None — the inverse of `phrase`.

    `Auftraggeber hinzufügen` → `Auftraggeber`. What it recovers is a wording the form's dictionary
    already has: a subject the wording table does not know can still be translated when the button it
    came from is keyed, because the label is the subject in a pattern this table owns."""
    pattern = PATTERNS.get(lang)
    text = (label or "").strip()
    if not pattern or not text:
        return None
    head, _, tail = (x.strip() for x in pattern.partition("{}"))
    if head:
        if not text.lower().startswith(head.lower()):
            return None
        text = text[len(head):].strip()
    if tail:
        if not text.lower().endswith(tail.lower()):
            return None
        text = text[:len(text) - len(tail)].strip()
    return capitalise(text) or None


def own_wording(text, form_translations):
    """{language: wording} for `text` as the form's own dictionary already translates it.

    Matched ignoring case and surrounding whitespace, because the case the sweep writes and the
    case the form keys need not agree: a form that translates its `Ordering Customer` heading keys exactly that, while the panel
    title derived from an Add button reads `Ordering customer`, and a key that differs by one letter
    is no key at all. A message identical to the text is not a translation and is left out."""
    out = {}
    for language, entries in (form_translations or {}).items():
        message = entries.get(text.casefold()) if text else None
        # Compared with the whitespace off on both sides. A forked key carries a trailing space
        # (PROBLEM-authrep-signature-title), and AAFD_019's Italian dictionary keys this very
        # wording to a lower-cased copy of the English — which the space stopped this test from
        # recognising as the non-translation it is, so `Aggiungere authorized representative` came
        # out on the button.
        if message and message.strip().casefold() != text.strip().casefold():
            out[language] = message
    return out


def casefold_index(entries):
    """{casefolded master: message} for one language's dictionary, for `own_wording`."""
    return {master.casefold(): message for master, message in entries.items()}


def translatable(form_locales, lang):
    """True when text written as its English master can be translated back for this form.

    The master is a dictionary key, so a form that ships no dictionary for its own language would be
    left reading the English on screen."""
    return (lang or "en") == "en" or any(LOCALE_LANG.get(l) == lang for l in form_locales)


def label_from_title(title, lang, form_locales, form_wording=None):
    """(label, {language: label}) for an Add button, given the panel's title.

    The button follows the title. Where the wording table knows the words, the title was written as
    its English master and keyed in the dictionary, so the label is the English phrase and carries the
    same entries — including one for the form's own language, which is what makes a German form read
    `Konto hinzufügen` instead of the master. Where the table knows nothing, the title stays as it is
    — and so does the label — but the form's own dictionary may still translate that title, and then
    the label is keyed from it: `Ordenante` gives `Añadir ordenante`. `form_wording` is what
    `own_wording` found for this title.
    """
    langs = [l for l in form_languages(form_locales) if l in PATTERNS and l != "en"]
    keyed = translatable(form_locales, lang)
    if not title:
        if keyed:
            return ADD_ONLY["en"], {l: ADD_ONLY[l] for l in langs}
        return ADD_ONLY.get(lang, ADD_ONLY["en"]), {}
    wording = subject_translations(title)
    # Only a title that IS the wording table's English master is phrased as the English master.
    # Anything else is somebody's own words — a hand-written title, or a subject the table does not
    # know — and is phrased as it stands, in the form's own language.
    if (wording.get("en") or "").casefold() != title.casefold():
        label = phrase(PATTERNS.get(lang, PATTERNS["en"]), title)
        own = {l: phrase(PATTERNS[l], w) for l, w in (form_wording or {}).items()
               if l in langs} if keyed else {}
        return label, own
    if keyed:
        return (phrase(PATTERNS["en"], title),
                {l: phrase(PATTERNS[l], wording.get(l, title)) for l in langs})
    return phrase(PATTERNS.get(lang, PATTERNS["en"]), wording.get(lang, title)), {}


def title_text(rep):
    """The panel's title as a dictionary key — `plain`, except that a TRAILING SPACE survives.

    A title that ends in a space has had its key forked on purpose: PROBLEM-authrep-signature-title
    spaces out the authorised representative's signature block so it can read `Berechtigte Person`
    in German while the data block stays `Vertretungsberechtigter`, and so the `accessibilityLabel`
    follows, which a rule cannot do. Carrying the space through is all this rule has to do about it
    — `classify` strips it for every wording decision and puts it back on the finished label, so the
    button is keyed `fd_Add authorized representative ` and `own_wording` reads the forked title's
    German back to compose `Berechtigte Person hinzufügen`. Collapsing the space instead keyed the
    button to the UNFORKED wording and stripped it off the button on the next pass; the panel and
    its button would have named the same party differently in German. Measured 2026-09-25: 128 titles in the corpus end in whitespace
    and not one of them is on a repeatable, so nothing else is affected."""
    raw = html.unescape(rep.attrs.get("jcr:title") or "")
    text = plain(raw)
    return text + " " if text and raw != raw.rstrip() else text


def classify(rep, buttons, form_locales, form_base=None, form_translations=None):
    """What should happen to one repeatable's Add button.

    `out_of_scope` when it has none, `ok` when every button it owns already reads the expected label,
    else `relabel`. The label is always the panel's own title phrased as an Add — the panel is where
    the wording is decided, and this rule only carries it over to the button, so the two can never
    disagree and a person only ever writes the title.
    """
    rep_name = rep.attrs.get("name") or ""
    owned = add_buttons_for(rep, buttons)
    if not owned:
        return {"verdict": "out_of_scope", "rep": rep_name}
    button = min(owned, key=lambda b: abs(b.start - rep.start))
    # The fork is held apart from the words. Everything the wording table does — recognising the
    # English master, translating the concept — is done on the STRIPPED title, because a trailing
    # space matches no table and sent this rule down the "somebody's own words" branch, which
    # composed `Authorized representative  hinzufügen`: the German verb, the English noun and two
    # spaces (AAFD_019, BAKT_019). The space goes back on at the end, where it belongs — the label
    # is the button's dictionary KEY, so it has to be the forked one.
    raw_title = title_text(rep)
    title, fork = raw_title.rstrip(), raw_title[len(raw_title.rstrip()):]
    lang = form_base or base_language(root_of(rep)) or "en"
    own = own_wording(raw_title if fork else title, form_translations) if title else None
    label, i18n = label_from_title(title, lang, form_locales, own)
    if fork:
        # The forked key OUTRANKS the wording table: the whole point of forking is that this panel
        # says something the concept does not — `Berechtigte Person` where the table says
        # `Vertretungsberechtigter`. Languages the fork did not translate keep what the table gave.
        langs = [l for l in form_languages(form_locales) if l in PATTERNS and l != "en"]
        label += fork
        i18n = {**i18n, **{l: phrase(PATTERNS[l], w) for l, w in (own or {}).items()
                           if l in langs}}
    out = {"rep": rep_name, "button": button.attrs.get("name"),
           "current": button.attrs.get("jcr:title"), "title": raw_title, "lang": lang,
           "label": label, "i18n": i18n}
    if len(owned) > 1:
        out["buttons"] = len(owned)
    # Case is a person's to decide: `Add Portfolio` beside a `Portfolio` panel is somebody insisting
    # on the capital, and rewriting it every run would be a fight nobody wins. Anything else is
    # rewritten. A button left this way is keyed under what it reads, so its dictionary entries are
    # written for that text too.
    reads = {(b.attrs.get("jcr:title") or "") for b in owned}
    out["verdict"] = "ok" if {t.casefold() for t in reads} == {label.casefold()} else "relabel"
    if out["verdict"] == "ok" and reads != {label}:
        out["cased"] = sorted(reads)[0]
    return out


def repeatables(root):
    return [n for n in walk(root) if is_repeatable(n)]


def classify_all(root, buttons, form_locales, form_base=None, form_translations=None):
    """[(repeatable, verdict), …] for a form, with a shared Add button left alone.

    Two repeatables often resolve to the same Add button — a panel and its signature twin sit under
    one — which is harmless while both want the same text. When their titles differ the button cannot
    satisfy both, and writing either would be undone on the next pass, so neither is written.
    PROBLEM-repeating-panel reports that panel for a person to split."""
    base = base_language(root) if form_base is None else form_base
    rows = [(rep, classify(rep, buttons, form_locales, base, form_translations))
            for rep in repeatables(root)]
    writes = lambda r: r["verdict"] in ("ok", "relabel")
    wanted = {}
    for rep, r in rows:
        if writes(r):
            for b in add_buttons_for(rep, buttons):
                wanted.setdefault(id(b), set()).add(r["label"])
    shared = {id(r) for rep, r in rows
              if writes(r) and any(len(wanted.get(id(b), ())) > 1 for b in add_buttons_for(rep, buttons))}
    return [(rep, {**r, "verdict": "shared"} if id(r) in shared else r) for rep, r in rows]
