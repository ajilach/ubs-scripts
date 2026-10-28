"""
Shared logic for PROBLEM-summary-step-redacto (rationale + extent: the registry entry).

Owner directive (2026-08-26): every form must render its Document of Record through
Redacto, i.e. carry `redactoSummary="true"` on the DAM asset. Reference confirmed by the
owner: the conversion engine with `use_summary = "true"` (`profiles/ubs/aem/root.xml:92-191`
for the panel, `dam.xml:31` for the flag). The carousel preview step is **kept** (owner
decision) — the target is the engine's `use_summary` shape minus the preview removal, which
is exactly what 109 already-correct forms look like.

`redactoSummary` alone is not the fix. The flag tells Redacto to build the DoR from the
server-rendered *summary* HTML, so a form that has no summary component produces an empty
DoR. Three things must hold together, and this module treats them as one state:

  1. DAM asset carries `redactoSummary="true"`.
  2. The root panel holds a `summaryPanel` step (tag `<summarypanel>`, the UBS custom
     panel) containing the `controls/summary` component named `summaryComponent`, plus
     the hidden `doroptionsubs` + `controls/metadata` controls (engine rule R5.4 — the
     DoR options and metadata must sit inside the summary subtree).
  3. The toolbar's `nextitemnav` runs `…control.summary.setSummaryData(guideRootPanel)`
     *before* `nextStep`, or the summary component is never populated.

Corpus 2026-08-26 (304 packages): 239 complete, 5 have (1)+(2) but not (3) — their DoR
renders from an unpopulated summary — and 60 have none of the three.

The `doroptionsubs` / `metadata` controls are **moved**, never synthesised: the metadata
control carries per-form data (`formrange_code`, `entities/item0/cdoks`) that cannot be
invented, and duplicating it would break PROBLEM-metadata-name / PROBLEM-metadata-languages,
which both assume one such control per form.

Neighbour rules that constrain the synthesised block (all verified against the deployed
reference form AAAV_019, which is the post-sweep ground truth, not the raw engine output):
  - PROBLEM-dor-exclusion-implies-summary — the panel carries `dorExclusion="true"`, so it
    must also carry `summaryExclusion="true"`. The three synthesised children carry neither.
  - PROBLEM-naming-conventions — `summaryPanel` is in that detector's EXEMPT_PANEL_NAMES;
    the children's fixed engine names are not author-named components.
  - PROBLEM-step-title-panel / -jump-to-field-button / -banking-* — all skip a panel whose
    tag starts with `summarypanel`, so the new step needs no step-title wrapper.
  - PROBLEM-toolbar-nav-handler — deliberately out of scope for `nextitemnav`; this rule
    only *prepends* a SCRIPTMODEL and never rewrites the existing `nextStep` entry (25
    forms carry an extra carousel-hide script, 1 uses the new `window.forms.ubs` namespace;
    both are preserved).
"""
import os
import re

from ui_translations import ui_text
import zipfile
from xml.parsers import expat

FRAG_AF = "jcr_root/content/forms/af/"
DAM_ROOT = "jcr_root/content/dam/formsanddocuments/"

SUMMARY_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/summary"
METADATA_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/metadata"
DOROPTS_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/dorOptionsUBS"
PANEL_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/panel"

SET_SUMMARY_DATA = "window.ajila.forms.ubs.control.summary.setSummaryData(guideRootPanel);"


# --------------------------------------------------------------------------- zip entries

def form_entry(z):
    """(inner_path, bytes) for the form's cq:Page .content.xml, or (None, None)."""
    cands = [n for n in z.namelist()
             if n.startswith(FRAG_AF) and n.endswith("/.content.xml")]
    for n in cands:
        data = z.read(n)
        if b'"cq:Page"' in data:
            return n, data
    if cands:
        return cands[0], z.read(cands[0])
    return None, None


def dam_entry(z):
    """(inner_path, bytes) for the DAM asset node that carries `dorType` — exactly one
    per package across the whole corpus (verified 304/304)."""
    for n in z.namelist():
        if n.startswith(DAM_ROOT) and n.endswith("/.content.xml"):
            data = z.read(n)
            if b"dorType" in data:
                return n, data
    return None, None


# --------------------------------------------------------------------------- XML spans

class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "start", "open_end", "end")

    def __init__(s, tag, attrs, parent, start):
        s.tag, s.attrs, s.children, s.parent, s.start = tag, attrs, [], parent, start
        s.open_end = None
        s.end = None


def open_tag_end(data, start):
    """Byte index just past the `>` closing the open tag at `start`, quote-aware.

    Rich-text and JSON attribute values hold a literal `>` (`_value="&lt;p>…"`,
    the `fd:click` SCRIPTMODEL arrays), so a naive scan for `>` corrupts edits.
    """
    i, inq = start + 1, False
    while i < len(data):
        c = data[i]
        if c == 0x22:
            inq = not inq
        elif c == 0x3e and not inq:
            return i + 1
        i += 1
    return len(data)


def parse_spans(data):
    """Parse `data` (bytes) into a Node tree carrying byte spans: `start` (the `<`),
    `open_end` (past the open tag's `>`) and `end` (past the element's last `>`)."""
    root = Node("__root__", {}, None, 0)
    stack = [root]
    p = expat.ParserCreate()

    def start(name, attrs):
        n = Node(name, attrs, stack[-1], p.CurrentByteIndex)
        n.open_end = open_tag_end(data, p.CurrentByteIndex)
        stack[-1].children.append(n)
        stack.append(n)

    def end(name):
        n = stack.pop()
        # A self-closing element ends where its open tag does. Detected from the bytes,
        # not from expat's CurrentByteIndex — for a self-closing tag that index is not
        # the element's own start, so trusting it makes the span run into the next
        # sibling (silently corrupting a move).
        n.end = n.open_end if data[n.open_end - 2:n.open_end] == b"/>" else \
            data.index(b">", p.CurrentByteIndex) + 1

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.Parse(data, True)
    return root


def walk(n):
    yield n
    for c in n.children:
        yield from walk(c)


def find(node, pred):
    for c in node.children:
        if pred(c):
            return c
        r = find(c, pred)
        if r:
            return r
    return None


def rootpanel_items(doc):
    gc = find(doc, lambda n: "guideContainer" in n.tag
              or n.attrs.get("guideNodeClass") == "guideContainer")
    rp = gc and (find(gc, lambda n: n.attrs.get("name") == "guideRootPanel")
                 or find(gc, lambda n: n.tag == "rootPanel"))
    if not rp:
        return None
    for c in rp.children:
        if c.tag == "items":
            return c
    return None


def indent_of(data, start):
    """The whitespace run immediately before the tag at `start`, as str."""
    i = start
    while i > 0 and data[i - 1] in b" \t":
        i -= 1
    return data[i:start].decode()


# --------------------------------------------------------------------------- state

def summary_panel(items):
    """The summary step, or None. Matched on the AF element `name` — the corpus is two
    lineages and the JCR tag is not a reliable marker on its own."""
    if items is None:
        return None
    for c in items.children:
        if c.attrs.get("name") == "summaryPanel":
            return c
    return None


#: The preview step's AF name in each lineage. The engine emits `preview`; the older
#: hand-built forms emit `PN_Preview` (5 _033 forms). Matching only the engine name is
#: exactly the "read one form, infer the structure" trap — the other detectors in this
#: repo carry the same alias pair in their SPECIAL_NAMES.
PREVIEW_NAMES = {"preview", "pn_preview"}

# The toolbar's preview button, by every spelling the corpus uses: the tag is `preview` or
# `previewbutton…` while the name is generated. Redacto renders the preview its own way, so the
# button is gone from the archetype — 159 toolbars still carry one.
def is_preview_button(node):
    tag = (node.tag or "").lower()
    return tag == "preview" or tag.startswith("previewbutton")


def toolbar(doc):
    return find(doc, lambda n: n.attrs.get("name") == "toolbar")


def preview_buttons(doc):
    tb = toolbar(doc)
    items = next((c for c in tb.children if c.tag == "items"), None) if tb is not None else None
    return [c for c in (items.children if items is not None else [])
            if is_preview_button(c)]


def preview_step(items):
    """The preview step the summary step is inserted before, or None."""
    if items is None:
        return None
    for c in items.children:
        if (c.attrs.get("name") or "").lower() in PREVIEW_NAMES \
                or c.tag.startswith("previewpanel"):
            return c
    return None


def _by_rt(node, rt):
    return [n for n in walk(node) if n.attrs.get("sling:resourceType") == rt]


def nextitemnav(doc):
    # UBS's older toolbars copy the button: AALT_019's Next is `<nextitemnav_copy name="nextitemnav">`
    # (59 packages), so match the name as well as the tag.
    return find(doc, lambda n: n.tag == "nextitemnav" or n.attrs.get("name") == "nextitemnav")


def nav_click_holder(nav):
    """The node carrying `fd:click` for the next button — an `<fd:scripts>` child on
    every form in the corpus, but fall back to the button itself."""
    if nav is None:
        return None
    for c in nav.children:
        if "fd:click" in c.attrs:
            return c
    return nav if "fd:click" in nav.attrs else None


def dictionary_locales(zip_path):
    """The locales this package ships a dictionary for."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return sorted({n.rsplit("/", 1)[1][:-4] for n in z.namelist()
                           if "/dictionary/" in n and n.endswith(".xml")} - {"sc", "tc"})
    except (zipfile.BadZipFile, OSError):
        return []


def state(zip_path):
    """The form's summary/Redacto state, read from the deployed package."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            fname, fdata = form_entry(z)
            dname, ddata = dam_entry(z)
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        return dict(EMPTY_STATE, error=str(exc))
    if fname is None or dname is None:
        return dict(EMPTY_STATE, error="missing form or DAM .content.xml")
    return state_from_bytes(fdata, ddata, dictionary_locales(zip_path))


EMPTY_STATE = {"redacto": False, "panel": False, "summary_component": False,
               "hook": False, "anchor": None, "doroptionsubs": 0, "metadata": 0,
               "nav_click": False, "panel_attrs": [], "panel_children": [],
               "preview_buttons": 0, "error": None}

# What the panel must carry, from the reference form. The four `jcr:*` audit attributes are per
# package and not compared.
PANEL_ATTRS = {
    "jcr:primaryType": "nt:unstructured",
    "jcr:title": "Summary of form information",
    "sling:resourceType": PANEL_RT,
    "dorExclusion": "true",
    "dorFieldStyling": "Default",
    "guideNodeClass": "guidePanel",
    "name": "summaryPanel",
    "summaryExclusion": "true",
    "textIsRich": "true",
    "validateOnStepCompletion": "{Boolean}false",
    "visible": "{Boolean}true",
}

# The children, in order, by the name each must carry. The converter leaves the last two under
# generated names (`doroptionsubs1742297849012`, `metadata1761809308844`), which nothing else in the
# repo can address.
PANEL_CHILDREN = ("messagebox_ElsigCheck", "summaryComponent", "submitErrorMessage",
                  "doroptionsubs", "metadataTextDraw")

CANONICAL_NAME_BY_RT = {DOROPTS_RT: "doroptionsubs", METADATA_RT: "metadataTextDraw"}


def panel_attr_gaps(panel, title):
    """[(attribute, current, wanted)] the panel does not match. `title` is the wanted heading, which
    is the English master where the form can translate it and its own words where it cannot."""
    wanted = dict(PANEL_ATTRS, **{"jcr:title": title})
    return [(k, panel.attrs.get(k), v) for k, v in wanted.items() if panel.attrs.get(k) != v]


def panel_child_gaps(panel):
    """[(what, name)] for the children missing from the panel or carrying a generated name."""
    items = next((c for c in panel.children if c.tag == "items"), None)
    kids = list(items.children) if items is not None else []
    present = {c.attrs.get("name") for c in kids}
    gaps, claimed = [], set()
    for node in kids:
        want = CANONICAL_NAME_BY_RT.get(node.attrs.get("sling:resourceType"))
        if not want:
            continue
        if node.attrs.get("name") == want:
            claimed.add(want)
            continue
        if want in present or want in claimed:
            # A second control of the same kind in the panel, under a generated name. Renaming it too
            # would leave two children called `doroptionsubs`, so the copy goes — except a metadata
            # carrying a form code the other one does not have, which is a decision for a person.
            gaps.append(("duplicate", node.attrs.get("name"), want))
            continue
        claimed.add(want)
        gaps.append(("rename", node.attrs.get("name"), want))
    named = {g[2] for g in gaps if g[0] != "duplicate"} | claimed
    for name in PANEL_CHILDREN:
        if name not in present and name not in named:
            gaps.append(("missing", name, name))
    return gaps


def state_from_bytes(fdata, ddata, locales=()):
    """The same state derived from raw member bytes — so the fixer can check its own
    output with the detector's logic instead of a second, drifting copy of it."""
    st = dict(EMPTY_STATE)
    st["redacto"] = b'redactoSummary="true"' in ddata

    doc = parse_spans(fdata)
    items = rootpanel_items(doc)
    if items is None:
        st["error"] = "no root panel items"
        return st

    panel = summary_panel(items)
    st["panel"] = panel is not None
    if panel is not None:
        st["summary_component"] = any(n.attrs.get("name") == "summaryComponent"
                                      for n in _by_rt(panel, SUMMARY_RT))
        st["panel_attrs"] = panel_attr_gaps(panel, wanted_title(fdata, locales))
        st["panel_children"] = panel_child_gaps(panel)
    st["preview_buttons"] = len(preview_buttons(doc))

    anchor = preview_step(items)
    st["anchor"] = anchor.attrs.get("name") if anchor is not None else None
    st["doroptionsubs"] = len(_by_rt(items, DOROPTS_RT))
    st["metadata"] = len(_by_rt(items, METADATA_RT))

    holder = nav_click_holder(nextitemnav(doc))
    st["nav_click"] = holder is not None
    if holder is not None:
        st["hook"] = "setSummaryData" in holder.attrs.get("fd:click", "")
    return st


SUMMARY_TITLE = PANEL_ATTRS["jcr:title"]


def wanted_title(fdata, locales=()):
    """The heading the panel must carry: the English master where the form can translate it, the
    form's own words where it ships no dictionary (see `ui_translations`)."""
    from repeatable_add_label import base_language, parse
    try:
        lang = base_language(parse(fdata.decode("utf-8", "replace")))
    except Exception:
        lang = "en"
    return ui_text(SUMMARY_TITLE, lang, locales)


def is_complete(st):
    return bool(st["redacto"] and st["panel"] and st["summary_component"] and st["hook"]
                and not st["panel_attrs"] and not st["panel_children"]
                and not st["preview_buttons"])


def bucket(st):
    """Which shape a form is in — the detector's classification surface."""
    if st["error"]:
        return "error"
    if is_complete(st):
        return "complete"
    if st["panel"] and st["summary_component"] and st["hook"] and st["redacto"]:
        return "off-shape"
    if st["panel"] and st["summary_component"]:
        return "missing-hook"
    if st["panel"] or st["redacto"]:
        return "partial"
    return "missing-panel"
