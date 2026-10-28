#!/usr/bin/env python3
"""
Enable the agreed features on UBS AEM Adaptive Form packages and report what was done.

For every package the script checks five features, fixes the missing ones, writes a fixed
copy, checks that copy again, and reports per feature whether it was already there, fixed,
not applicable, or could not be fixed.

  redacto_dor    Redacto DoR        DAM metadata `redactoSummary="true"`
  summary        HTML preview /     a `summaryPanel` step holding the summary component; a
                 summary            missing one is built before the preview step, with the
                                    form's own `doroptionsubs` and metadata controls moved in
  save_as_draft  Save as draft      DAM metadata `menuOptions` contains
                                    `ajila-forms-ubs-menu-option-save`
  print_partial  Print incomplete   DAM metadata `printPartialMode="true"`, Germany forms
                                    (`afforms_germany_all`) only; other forms are "n/a"
  input_fields   Show only input    the FormMetadata fragment is the first, hidden wizard step;
                 fields             the Forms Workbench button writes to its field
                                    `txtShowOnlyInputFields`

The summary step and the FormMetadata step are built the same way as the sweeps
`summary-step-redacto` and `formmetadata-step` in ajila-forms-conversion-feedback
(`.claude/scripts/set_summary_step.py`, `set_formmetadata_step.py`, commit a2421999), whose
code this script contains. Only the affected attributes and nodes change; the rest of each
file is kept byte for byte. Fixed packages go to --out as copies; the input ZIPs are never
modified. Python 3.8+, standard library only, no other files needed.

Usage:
    python3 fix_form_features.py PACKAGE.zip [...] --out FIXED_DIR
    python3 fix_form_features.py FOLDER --out FIXED_DIR --csv report.csv
    python3 fix_form_features.py FOLDER --dry-run          # report only, write nothing

Exit code: 0 if every package has every feature after fixing, 1 if any package could not
be fixed, 2 on bad usage.
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from xml.parsers import expat

DAM_ROOT = "jcr_root/content/dam/formsanddocuments/"
FORM_ROOT = "jcr_root/content/forms/af/"
SAVE_OPTION = "ajila-forms-ubs-menu-option-save"
FORMMETADATA_FRAGMENT = "/content/dam/formsanddocuments/afforms_global_fragmentlib/formmetadata"
GERMANY_FOLDER = "afforms_germany_all"

SUMMARY_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/summary"
METADATA_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/metadata"
DOROPTS_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/dorOptionsUBS"
PANEL_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/panel"
MESSAGEBOX_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/messagebox"
GRID_LAYOUT = "fd/af/layouts/gridFluidLayout2"
SET_SUMMARY_DATA = "window.ajila.forms.ubs.control.summary.setSummaryData(guideRootPanel);"

FEATURES = (
    ("redacto_dor", "Redacto DoR"),
    ("summary", "HTML preview/summary"),
    ("save_as_draft", "Save as draft"),
    ("print_partial", "Print incomplete"),
    ("input_fields", "Show only input fields"),
)

# The summary fixer completes in two passes (the second names the moved controls); a third
# pass must change nothing.
SUMMARY_PASSES = 3


class FixError(Exception):
    """The package cannot be read, or cannot be brought to the target state."""


# =========================================================================== checks

def parse_prefixed(data):
    """Parse JCR FileVault XML, giving tags and attribute names back in their prefixed form
    (`sling:resourceType`, `fd:click`) instead of `{uri}local`."""
    prefix_of = {}
    for _event, (prefix, uri) in ET.iterparse(io.BytesIO(data), events=("start-ns",)):
        prefix_of[uri] = prefix
    root = ET.fromstring(data)

    def name(qname):
        if qname.startswith("{"):
            uri, local = qname[1:].split("}", 1)
            prefix = prefix_of.get(uri)
            return "%s:%s" % (prefix, local) if prefix else local
        return qname

    for el in root.iter():
        el.tag = name(el.tag)
        el.attrib = {name(k): v for k, v in el.attrib.items()}
    return root


def find_first(root, pred):
    return next((el for el in root.iter() if pred(el)), None)


def multi_value(raw):
    """A JCR multi-value property `[a,b]` as a list (single values as a one-item list)."""
    if raw is None:
        return []
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [v.strip() for v in raw.split(",") if v.strip()]


def is_germany(form_path):
    return GERMANY_FOLDER in form_path.split("/")


def wizard_steps(form_root):
    """The children of the root panel's `items` (the wizard steps), or None."""
    panel = find_first(form_root, lambda el: el.attrib.get("name") == "guideRootPanel"
                       or el.tag == "rootPanel")
    return next((c for c in panel if c.tag == "items"), None) if panel is not None else None


def has_summary(form_root):
    # No check on the Next button: the UBS app's `navigation.nextStep` fills the summary
    # itself whenever the page holds a summary component (since 2026-05-19).
    steps = wizard_steps(form_root)
    panel = next((s for s in steps if s.attrib.get("name") == "summaryPanel"), None) \
        if steps is not None else None
    return panel is not None and any(el.attrib.get("sling:resourceType") == SUMMARY_RT
                                     and el.attrib.get("name") == "summaryComponent"
                                     for el in panel.iter())


def has_formmetadata_step(form_root):
    steps = wizard_steps(form_root)
    return steps is not None and any(
        s.attrib.get("fragRef", "").rstrip("/") == FORMMETADATA_FRAGMENT for s in steps)


def feature_states(dam, form, form_path):
    """{feature: True | False | None}; None means the feature does not apply to this form."""
    meta = find_first(parse_prefixed(dam), lambda el: el.tag == "metadata")
    if meta is None:
        raise FixError("DAM asset has no <metadata> node")
    meta = meta.attrib
    form_root = parse_prefixed(form)
    return {
        "redacto_dor": meta.get("redactoSummary") == "true",
        "summary": has_summary(form_root),
        "save_as_draft": SAVE_OPTION in multi_value(meta.get("menuOptions")),
        "print_partial": meta.get("printPartialMode") == "true" if is_germany(form_path) else None,
        "input_fields": has_formmetadata_step(form_root),
    }


# =========================================================================== XML spans
# Byte-offset tree used by the structural fixes: edits are splices into the raw bytes, so
# the rest of the file is never re-serialised.

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
        # not from expat's CurrentByteIndex: for a self-closing tag that index is not the
        # element's own start, so trusting it makes the span run into the next sibling.
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


def line_indent(data, offset):
    """The indentation of the line containing `offset`."""
    line_start = data.rfind(b"\n", 0, offset) + 1
    return re.match(rb"[ \t]*", data[line_start:offset]).group(0).decode()


# =========================================================================== summary step: state
# Three parts make Redacto render the DoR from a populated summary, so they are one state:
#   1. `redactoSummary="true"` on the DAM asset,
#   2. a `summaryPanel` step holding the summary component `summaryComponent`, plus the
#      hidden `doroptionsubs` and metadata controls (they must sit inside the summary),
#   3. the toolbar's `nextitemnav` calling `setSummaryData(guideRootPanel)` before `nextStep`.
# The panel must match the reference form (AAOS_033), bar the per-form data.

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
SUMMARY_TITLE = PANEL_ATTRS["jcr:title"]
# The heading in the form's own language, for a form that ships no dictionary in it.
SUMMARY_TITLE_IN = {
    "de": "Zusammenfassung der Formulardaten",
    "it": "Riepilogo delle informazioni del modulo",
    "fr": "Résumé des informations du formulaire",
    "es": "Resumen de la información del formulario",
}
LOCALE_LANG = {"de": "de", "de-ch": "de", "it": "it", "it-ch": "it", "es": "es", "sp": "es",
               "fr": "fr", "en": "en", "en-us": "en", "en-gb": "en"}
MASTER_ATTR = "formrange_afmasterlanguage"
MASTER_LANG = {"EN": "en", "DE": "de", "IT": "it", "SP": "es", "ES": "es", "FR": "fr"}

# The children, in order, by the name each must carry. The converter leaves the last two under
# generated names (`doroptionsubs1742297849012`, `metadata1761809308844`).
PANEL_CHILDREN = ("messagebox_ElsigCheck", "summaryComponent", "submitErrorMessage",
                  "doroptionsubs", "metadataTextDraw")
CANONICAL_NAME_BY_RT = {DOROPTS_RT: "doroptionsubs", METADATA_RT: "metadataTextDraw"}
PREVIEW_NAMES = {"preview", "pn_preview"}


def summary_panel(items):
    """The summary step, or None, matched on the AF element `name`."""
    if items is None:
        return None
    for c in items.children:
        if c.attrs.get("name") == "summaryPanel":
            return c
    return None


def is_preview_button(node):
    tag = (node.tag or "").lower()
    return tag == "preview" or tag.startswith("previewbutton")


def preview_buttons(doc):
    tb = find(doc, lambda n: n.attrs.get("name") == "toolbar")
    items = next((c for c in tb.children if c.tag == "items"), None) if tb is not None else None
    return [c for c in (items.children if items is not None else []) if is_preview_button(c)]


def preview_step(items):
    """The preview step the summary step is inserted before, or None."""
    if items is None:
        return None
    for c in items.children:
        if (c.attrs.get("name") or "").lower() in PREVIEW_NAMES \
                or c.tag.startswith("previewpanel"):
            return c
    return None


def by_resource_type(node, rt):
    return [n for n in walk(node) if n.attrs.get("sling:resourceType") == rt]


def nextitemnav(doc):
    # Older toolbars copy the button (`<nextitemnav_copy name="nextitemnav">`): match both.
    return find(doc, lambda n: n.tag == "nextitemnav" or n.attrs.get("name") == "nextitemnav")


def nav_click_holder(nav):
    """The node carrying `fd:click` for the Next button: an `<fd:scripts>` child, or the
    button itself."""
    if nav is None:
        return None
    for c in nav.children:
        if "fd:click" in c.attrs:
            return c
    return nav if "fd:click" in nav.attrs else None


def dictionary_locales(zip_path):
    """The locales this package ships a dictionary for."""
    with zipfile.ZipFile(zip_path) as z:
        return sorted({n.rsplit("/", 1)[1][:-4] for n in z.namelist()
                       if "/dictionary/" in n and n.endswith(".xml")} - {"sc", "tc"})


def wanted_title(fdata, locales):
    """The heading the summary panel must carry: the English master where the form ships a
    dictionary in its own language (the master is then a dictionary key), else the wording
    in the form's own language. The language is the one the form's metadata control declares."""
    lang = None
    for n in walk(parse_spans(fdata)):
        if METADATA_RT in (n.attrs.get("sling:resourceType") or ""):
            code = (n.attrs.get(MASTER_ATTR) or "").strip().upper()
            if code:
                lang = MASTER_LANG.get(code)
                break
    if lang is None:
        raise FixError("the form's metadata control declares no known master language "
                       "(%s), so the summary heading's language is unknown" % MASTER_ATTR)
    if lang == "en" or any(LOCALE_LANG.get(loc) == lang for loc in locales):
        return SUMMARY_TITLE
    return SUMMARY_TITLE_IN.get(lang, SUMMARY_TITLE)


def panel_attr_gaps(panel, title):
    """[(attribute, current, wanted)] the panel does not match."""
    wanted = dict(PANEL_ATTRS, **{"jcr:title": title})
    return [(k, panel.attrs.get(k), v) for k, v in wanted.items() if panel.attrs.get(k) != v]


def panel_child_gaps(panel):
    """[(what, name, wanted)] for the children missing from the panel or carrying a
    generated name."""
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
            # A second control of the same kind under a generated name: the copy goes, except
            # a metadata carrying a different form code, which is a decision for a person.
            gaps.append(("duplicate", node.attrs.get("name"), want))
            continue
        claimed.add(want)
        gaps.append(("rename", node.attrs.get("name"), want))
    named = {g[2] for g in gaps if g[0] != "duplicate"} | claimed
    for name in PANEL_CHILDREN:
        if name not in present and name not in named:
            gaps.append(("missing", name, name))
    return gaps


def summary_state(fdata, ddata, locales):
    """The form's summary/Redacto state from the raw form and DAM bytes."""
    st = {"redacto": b'redactoSummary="true"' in ddata, "panel": False,
          "summary_component": False, "hook": False, "panel_attrs": [], "panel_children": [],
          "preview_buttons": 0}
    doc = parse_spans(fdata)
    items = rootpanel_items(doc)
    if items is None:
        raise FixError("form has no root panel items")
    panel = summary_panel(items)
    st["panel"] = panel is not None
    if panel is not None:
        st["summary_component"] = any(n.attrs.get("name") == "summaryComponent"
                                      for n in by_resource_type(panel, SUMMARY_RT))
        st["panel_attrs"] = panel_attr_gaps(panel, wanted_title(fdata, locales))
        st["panel_children"] = panel_child_gaps(panel)
    st["preview_buttons"] = len(preview_buttons(doc))
    holder = nav_click_holder(nextitemnav(doc))
    if holder is not None:
        st["hook"] = "setSummaryData" in holder.attrs.get("fd:click", "")
    return st


def summary_complete(st):
    return bool(st["redacto"] and st["panel"] and st["summary_component"] and st["hook"]
                and not st["panel_attrs"] and not st["panel_children"]
                and not st["preview_buttons"])


# =========================================================================== summary step: build

CREATED = "{Date}2025-01-01T00:00:00.000Z"
ELSIG_ATTRS = [
    ("jcr:primaryType", "nt:unstructured"),
    ("sling:resourceType", MESSAGEBOX_RT),
    ("css", "messagebox-ElsigCheck ubs-margin-10"),
    ("guideNodeClass", "guideTextDraw"),
    ("hideTitle", "{Boolean}true"),
    ("i18nBodyId", "ajila-forms-ubs-signature-level-error"),
    ("name", "messagebox_ElsigCheck"),
    ("visible", "{Boolean}true"),
]
SUBMIT_ERR_ATTRS = [
    ("jcr:primaryType", "nt:unstructured"),
    ("sling:resourceType", MESSAGEBOX_RT),
    ("guideNodeClass", "guideTextDraw"),
    ("messageboxBody", "&lt;p>The form could not be sent. Please try again later.&lt;/p>"),
    ("messageboxTitle", "Submission failed"),
    ("messageboxType", "{Long}4"),
    ("name", "submitErrorMessage"),
    ("visible", "{Boolean}false"),
]
LAYOUT_ATTRS = [
    ("jcr:primaryType", "nt:unstructured"),
    ("sling:resourceType", GRID_LAYOUT),
    ("columns", "1"),
    ("dorLayoutType", "columnar"),
    ("dorNumCols", "1"),
    ("nonNavigable", "{Boolean}true"),
    ("toolbarPosition", "Bottom"),
]
DOROPTS_ATTRS = [
    ("jcr:primaryType", "nt:unstructured"),
    ("sling:resourceType", DOROPTS_RT),
    ("_value", "Further configurations for the document of record"),
    ("dorExclusion", "true"),
    ("guideNodeClass", "guideTextDraw"),
    ("name", "doroptionsubs"),
    ("summaryExclusion", "true"),
    ("visible", "false"),
]
SYNTHESISED = {"messagebox_ElsigCheck": ELSIG_ATTRS, "submitErrorMessage": SUBMIT_ERR_ATTRS,
               "doroptionsubs": DOROPTS_ATTRS}
TAG_FOR = {"messagebox_ElsigCheck": "messagebox_ElsigCheck",
           "submitErrorMessage": "messagebox_SubmissionError",
           "doroptionsubs": "doroptionsubs"}

# One SCRIPTMODEL entry for the fd:click array, in FileVault's on-disk escaping: `"` written
# as &quot; and every JSON comma escaped as `\,`. Built by substitution so it matches the
# corpus byte for byte.
CLICK_ENTRY = (
    '{&quot;script&quot;:{&quot;field&quot;:&quot;{FIELD}&quot;\\,'
    '&quot;event&quot;:&quot;Click&quot;\\,'
    '&quot;model&quot;:{&quot;nodeName&quot;:&quot;EVENT_SCRIPTS&quot;}\\,'
    '&quot;content&quot;:&quot;{CONTENT}&quot;}\\,'
    '&quot;nodeName&quot;:&quot;SCRIPTMODEL&quot;\\,'
    '&quot;version&quot;:1\\,&quot;enabled&quot;:true}'
)
DEFAULT_FIELD = "guide.guideRootPanel.toolbar.nextitemnav"
FIELD_RE = re.compile(r'&quot;field&quot;:&quot;([^&]+)&quot;')


def attr_indent(data, node, fallback):
    """The column the node's attributes are written at, or `fallback` when the open tag is a
    single line. Read from the form itself: the corpus has two formatting lineages."""
    nl = data.find(b"\n", node.start, node.open_end)
    if nl < 0:
        return fallback
    i = nl + 1
    while i < len(data) and data[i] in b" \t":
        i += 1
    return data[nl + 1:i].decode()


def render(tag, attrs, indent, attr_ind, close):
    """One element, attributes one per line. `close` is '/>' or '>'."""
    lines = ["%s<%s" % (indent, tag)]
    lines += ['%s%s="%s"' % (attr_ind, k, v) for k, v in attrs]
    lines[-1] += close
    return "\n".join(lines)


def reindent(text, delta):
    """Shift every line's leading whitespace by `delta` columns. Only lines that begin with
    whitespace followed by `<` or an `attr="` token are touched, so a rich-text value spanning
    lines is left alone."""
    if delta == 0:
        return text
    out = []
    for i, line in enumerate(text.split("\n")):
        if i == 0 or not re.match(r'\s+(<|[\w:.\-]+=")', line):
            out.append(line)
            continue
        stripped = line.lstrip(" ")
        pad = len(line) - len(stripped)
        out.append(" " * max(0, pad + delta) + stripped)
    return "\n".join(out)


def build_panel(data, anchor, moved_nodes, author, title):
    """The whole `<summarypanel>` element as text, indented to match `anchor`."""
    base = indent_of(data, anchor.start)
    a_ind = attr_indent(data, anchor, base + "    ")
    # Child indents come from the anchor's own items, so the moved nodes need no reindent on
    # the common shape.
    items = next((c for c in anchor.children if c.tag == "items"), None)
    items_ind = indent_of(data, items.start) if items is not None else base + "    "
    items_attr = attr_indent(data, items, items_ind + "    ") if items is not None \
        else items_ind + "    "
    kids = [c for c in items.children] if items is not None else []
    child_ind = indent_of(data, kids[0].start) if kids else items_ind + "    "
    child_attr = attr_indent(data, kids[0], child_ind + "    ") if kids else child_ind + "    "

    panel_attrs = [
        ("jcr:created", CREATED),
        ("jcr:createdBy", author),
        ("jcr:lastModified", CREATED),
        ("jcr:lastModifiedBy", author),
        ("jcr:primaryType", "nt:unstructured"),
        ("jcr:title", title),
        ("sling:resourceType", PANEL_RT),
        ("dorExclusion", "true"),
        ("dorFieldStyling", "Default"),
        ("summaryExclusion", "true"),
        ("guideNodeClass", "guidePanel"),
        ("name", "summaryPanel"),
        ("textIsRich", "true"),
        ("validateOnStepCompletion", "{Boolean}false"),
        ("visible", "{Boolean}true"),
    ]
    summary_attrs = [
        ("jcr:created", CREATED),
        ("jcr:createdBy", author),
        ("jcr:lastModified", CREATED),
        ("jcr:lastModifiedBy", author),
        ("jcr:primaryType", "nt:unstructured"),
        ("jcr:title", "Summary"),
        ("sling:resourceType", SUMMARY_RT),
        ("autofillFieldKeyword", "name"),
        ("css", "widget_ajila_forms_summary"),
        ("dorFieldStyling", "Default"),
        ("guideNodeClass", "guideTextBox"),
        ("name", "summaryComponent"),
        ("replaceEmptyValues", "true"),
        ("showStaticText", "true"),
        ("textIsRich", "[true,true,true]"),
    ]

    parts = [render("summarypanel", panel_attrs, base, a_ind, ">"),
             render("items", [("jcr:primaryType", "nt:unstructured"),
                              ("sling:resourceType", GRID_LAYOUT)],
                    items_ind, items_attr, ">"),
             render("messagebox_ElsigCheck", ELSIG_ATTRS, child_ind, child_attr, "/>"),
             render("summary", summary_attrs, child_ind, child_attr, "/>"),
             render("messagebox_SubmissionError", SUBMIT_ERR_ATTRS, child_ind,
                    child_attr, "/>")]
    for node, text in moved_nodes:
        # `text` starts at the `<`, so the first line carries no indent of its own.
        own = indent_of(data, node.start)
        parts.append(child_ind + reindent(text, len(child_ind) - len(own)))
    parts.append("%s</items>" % items_ind)
    parts.append(render("layout", LAYOUT_ATTRS, items_ind, items_attr, "/>"))
    parts.append("%s</summarypanel>" % base)
    return "\n".join(parts)


def rewrite_open_tag(data, node, wanted, close):
    """The node's open tag with `wanted` applied: values replaced where they differ, missing
    attributes inserted in FileVault's order. Audit attributes are kept."""
    start, end = node.start, open_tag_end(data, node.start)
    attrs = dict(re.findall(r'([\w:.\-]+)="([^"]*)"', data[start:end].decode("utf-8")))
    attrs.update(wanted)
    ordered = sorted(attrs.items(), key=lambda kv: (not kv[0].startswith("jcr:"),
                                                    not kv[0].startswith("sling:"), kv[0]))
    indent = indent_of(data, node.start)
    a_ind = attr_indent(data, node, indent + "    ")
    return start, end, render(node.tag, ordered, "", a_ind, close).lstrip("\n").encode()


def inside(panel, node):
    return any(n is node for n in walk(panel))


def in_preview(items, node):
    steps = [c for c in items.children
             if (c.attrs.get("name") or "").lower() in ("preview", "pn_preview")]
    return any(inside(step, node) for step in steps)


def normalise_panel(data, panel, st, items):
    """(edits, actions, flags) bringing an existing summary panel to the reference shape."""
    edits, actions, flags = [], [], []

    if st["panel_attrs"]:
        close = ">" if panel.children else "/>"
        wanted = dict(PANEL_ATTRS)
        wanted["jcr:title"] = next((w for a, _c, w in st["panel_attrs"] if a == "jcr:title"),
                                   panel.attrs.get("jcr:title") or SUMMARY_TITLE)
        start, end, text = rewrite_open_tag(data, panel, wanted, close)
        edits.append((start, "rep", (end, text)))
        actions.append("panel-attrs")

    kids_node = next((c for c in panel.children if c.tag == "items"), None)
    kids = list(kids_node.children) if kids_node is not None else []
    for kind, current, wanted in st["panel_children"]:
        if kind == "duplicate":
            node = next((c for c in kids if c.attrs.get("name") == current), None)
            if node is None:
                continue
            twin = next((c for c in kids if c.attrs.get("name") == wanted), None)
            if (node.attrs.get("formrange_code") and twin is not None
                    and node.attrs.get("formrange_code") != twin.attrs.get("formrange_code")):
                flags.append("two %s controls with different form codes" % wanted)
                continue
            start = node.start - len(indent_of(data, node.start))
            end = node.end + (1 if data[node.end:node.end + 1] == b"\n" else 0)
            edits.append((start, "cut", end))
            actions.append("dropped-duplicate:" + wanted)
            continue
        if kind == "rename":
            node = next((c for c in kids if c.attrs.get("name") == current), None)
            if node is None:
                continue
            start, end = node.start, open_tag_end(data, node.start)
            tag = data[start:end].decode("utf-8")
            new = re.sub(r'(?<![\w:.\-])name="[^"]*"', 'name="%s"' % wanted, tag, count=1)
            edits.append((start, "rep", (end, new.encode())))
            actions.append("rename:" + wanted)
            continue
        # missing: move the form's own control in, or synthesise the ones that carry no data
        rt = {"doroptionsubs": DOROPTS_RT, "metadataTextDraw": METADATA_RT}.get(wanted)
        outside = [n for n in walk(items)
                   if n.attrs.get("sling:resourceType") == rt and not inside(panel, n)] if rt else []
        # A copy in the preview step is never the one to keep while another exists elsewhere.
        if len(outside) > 1:
            outside = [n for n in outside if not in_preview(items, n)] or outside
        anchor = kids[-1] if kids else None
        if outside:
            if len(outside) > 1:
                flags.append("%d %s controls outside the panel" % (len(outside), wanted))
                continue
            node = outside[0]
            text = data[node.start:node.end].decode("utf-8", "replace")
            own = indent_of(data, node.start)
            child_ind = indent_of(data, anchor.start) if anchor is not None else own
            cut_from = node.start - len(own)
            cut_to = node.end + (1 if data[node.end:node.end + 1] == b"\n" else 0)
            edits.append((cut_from, "cut", cut_to))
            moved = child_ind + reindent(text, len(child_ind) - len(own))
            moved = re.sub(r'(?<![\w:.\-])name="[^"]*"', 'name="%s"' % wanted, moved, count=1)
            at = kids_node.end - len("</items>") - len(indent_of(data, kids_node.end))
            edits.append((at, "ins", (moved + "\n").encode()))
            actions.append("moved:" + wanted)
        elif wanted in SYNTHESISED and kids_node is not None:
            child_ind = indent_of(data, anchor.start) if anchor is not None else "    "
            child_attr = attr_indent(data, anchor, child_ind + "    ") if anchor is not None \
                else child_ind + "    "
            block = render(TAG_FOR[wanted], SYNTHESISED[wanted], child_ind, child_attr, "/>")
            at = kids_node.end - len("</items>") - len(indent_of(data, kids_node.end))
            edits.append((at, "ins", (block.lstrip("\n") + "\n").encode()))
            actions.append("added:" + wanted)
        else:
            flags.append("no %s control to move in" % wanted)
    return edits, actions, flags


def dam_insert_redacto(ddata):
    """(pos, text) inserting `redactoSummary="true"` into the DAM metadata node in FileVault's
    alphabetical position, or None when already set."""
    if b'redactoSummary="true"' in ddata:
        return None
    text = ddata.decode()
    m = re.search(r'<metadata\b', text)
    if not m:
        return None
    end = open_tag_end(ddata, m.start())
    tag = text[m.start():end]
    attrs = list(re.finditer(r'\n(\s+)([\w:.\-]+)="', tag))
    if not attrs:
        return None
    ind = attrs[-1].group(1)
    for am in attrs:
        # Namespaced attributes sort ahead of the plain ones in FileVault's output.
        if ":" not in am.group(2) and am.group(2) > "redactoSummary":
            return (m.start() + am.start(), '\n%sredactoSummary="true"' % ind)
    close = end - 2 if text[end - 2:end] == "/>" else end - 1
    return (close, '\n%sredactoSummary="true"' % ind)


def click_prepend(data, holder):
    """(pos, text) prepending the setSummaryData SCRIPTMODEL to the holder's fd:click array,
    or None when the hook is already there or there is no array to prepend to."""
    seg = data[holder.start:holder.open_end].decode("utf-8", "replace")
    m = re.search(r'fd:click="\[', seg)
    if not m or "setSummaryData" in seg:
        return None
    fm = FIELD_RE.search(seg)
    entry = CLICK_ENTRY.replace("{FIELD}", fm.group(1) if fm else DEFAULT_FIELD) \
                       .replace("{CONTENT}", SET_SUMMARY_DATA)
    return (holder.start + m.end(), entry + ",")


def summary_pass(data, ddata, locales):
    """One pass of the summary fix. Returns (form, dam, actions, flag or None)."""
    doc = parse_spans(data)
    items = rootpanel_items(doc)
    if items is None:
        return data, ddata, [], "no root panel items"

    actions, edits = [], []   # edits: (pos, kind, ...) applied right to left

    if summary_panel(items) is None:
        anchor = preview_step(items)
        if anchor is None:
            return data, ddata, actions, "no preview step to anchor the summary step to"
        take = []
        for rt in (DOROPTS_RT, METADATA_RT):
            found = by_resource_type(items, rt)
            if len(found) != 1:
                return data, ddata, actions, "expected exactly 1 %s control, found %d" % (
                    rt.rsplit("/", 1)[-1], len(found))
            take.append(found[0])
        moved_nodes = [(n, data[n.start:n.end].decode("utf-8", "replace")) for n in take]
        author = anchor.attrs.get("jcr:createdBy", "blueprint")
        block = build_panel(data, anchor, moved_nodes, author, wanted_title(data, locales))
        for n in take:                    # cut each moved node together with its own line
            start = n.start - len(indent_of(data, n.start))
            end = n.end + (1 if data[n.end:n.end + 1] == b"\n" else 0)
            edits.append((start, "cut", end))
        indent = indent_of(data, anchor.start)
        edits.append((anchor.start - len(indent), "ins", (block + "\n").encode()))
        actions.append("panel")

    panel = summary_panel(items)
    if panel is not None:
        p_edits, p_actions, p_flags = normalise_panel(
            data, panel, summary_state(data, ddata, locales), items)
        if p_flags:
            return data, ddata, actions, "; ".join(p_flags)
        edits.extend(p_edits)
        actions.extend(p_actions)

    # Redacto renders the preview its own way, so the old toolbar button goes.
    for button in preview_buttons(doc):
        start = button.start - len(indent_of(data, button.start))
        end = button.end + (1 if data[button.end:button.end + 1] == b"\n" else 0)
        edits.append((start, "cut", end))
        actions.append("preview-button")

    holder = nav_click_holder(nextitemnav(doc))
    if holder is None:
        return data, ddata, actions, "no nextitemnav fd:click to hook"
    cp = click_prepend(data, holder)
    if cp:
        edits.append((cp[0], "ins", cp[1].encode()))
        actions.append("hook")

    out = data
    for e in sorted(edits, key=lambda x: x[0], reverse=True):
        if e[1] == "cut":
            out = out[:e[0]] + out[e[2]:]
        elif e[1] == "rep":
            out = out[:e[0]] + e[2][1] + out[e[2][0]:]
        else:
            out = out[:e[0]] + e[2] + out[e[0]:]

    dout = ddata
    di = dam_insert_redacto(ddata)
    if di:
        dout = ddata[:di[0]] + di[1].encode() + ddata[di[0]:]
        actions.append("redacto")
    return out, dout, actions, None


def fix_summary(form, dam, locales):
    """Build the summary step, repeated until a pass changes nothing.
    Returns (form, dam, [actions])."""
    actions = []
    for _ in range(SUMMARY_PASSES):
        new_form, new_dam, done, flag = summary_pass(form, dam, locales)
        if flag:
            raise FixError("summary step could not be built: %s" % flag)
        if (new_form, new_dam) == (form, dam):
            break
        actions += [a for a in done if a not in actions]
        form, dam = new_form, new_dam
    if not summary_complete(summary_state(form, dam, locales)):
        raise FixError("summary step could not be completed")
    return form, dam, actions


# =========================================================================== FormMetadata step
# The FormMetadata fragment as the form's first wizard step, in the canonical shape
# (reference AAOV_033): a fragment node directly under the root panel's items, hidden and
# excluded from the DoR. It holds `txtShowOnlyInputFields` among other runtime fields.

FORMMETADATA_ATTRS = {
    "jcr:primaryType": "nt:unstructured",
    "jcr:title": "FormMetadata",
    "sling:resourceType": PANEL_RT,
    "dorExcludeDescription": "true",
    "dorExcludeTitle": "true",
    "dorExclusion": "true",
    "fragRef": FORMMETADATA_FRAGMENT,
    "guideNodeClass": "guidePanel",
    "name": "FormMetadata",
    "textIsRich": "true",
    "visible": "false",
}


def is_formmetadata(node):
    return (node.attrs.get("fragRef") or "").rstrip("/") == FORMMETADATA_FRAGMENT


def formmetadata_deviations(node):
    return {k: node.attrs.get(k) for k, v in FORMMETADATA_ATTRS.items()
            if node.attrs.get(k) != v}


def formmetadata_xml(tag, indent):
    """The canonical FormMetadata subtree, indented to sit at `indent`."""
    i2 = a = indent + "    "
    return (
        '<%s\n' % tag
        + '%sjcr:primaryType="nt:unstructured"\n' % a
        + '%sjcr:title="FormMetadata"\n' % a
        + '%ssling:resourceType="%s"\n' % (a, PANEL_RT)
        + '%sdorExcludeDescription="true"\n' % a
        + '%sdorExcludeTitle="true"\n' % a
        + '%sdorExclusion="true"\n' % a
        + '%sfragRef="%s"\n' % (a, FORMMETADATA_FRAGMENT)
        + '%sguideNodeClass="guidePanel"\n' % a
        + '%sname="FormMetadata"\n' % a
        + '%stextIsRich="true"\n' % a
        + '%svisible="false">\n' % a
        + '%s<layout\n' % i2
        + '%s    jcr:primaryType="nt:unstructured"\n' % i2
        + '%s    sling:resourceType="%s"\n' % (i2, GRID_LAYOUT)
        + '%s    columns="1"\n' % i2
        + '%s    dorLayoutType="columnar"\n' % i2
        + '%s    dorNumCols="1"\n' % i2
        + '%s    nonNavigable="{Boolean}true"\n' % i2
        + '%s    toolbarPosition="Bottom"/>\n' % i2
        + '%s<items\n' % i2
        + '%s    jcr:primaryType="nt:unstructured"\n' % i2
        + '%s    sling:resourceType="%s"/>\n' % (i2, GRID_LAYOUT)
        + '%s</%s>' % (indent, tag)
    )


def normalise_formmetadata_tag(tag):
    """Every canonical attribute set on this open tag (str). A missing one goes on its own
    line, or inline before the closing `>` / `/>` on a single-line tag. Returns
    (tag, [changed attributes])."""
    changed = []
    for attr, want in FORMMETADATA_ATTRS.items():
        cur = re.search(r'(?<![\w:.\-])%s="([^"]*)"' % re.escape(attr), tag)
        if cur:
            if cur.group(1) == want:
                continue
            changed.append(attr)
            tag = tag[:cur.start()] + '%s="%s"' % (attr, want) + tag[cur.end():]
            continue
        changed.append(attr)
        nl = tag.find("\n")
        if nl == -1:
            close = 2 if tag.rstrip().endswith("/>") else 1
            tag = '%s %s="%s"%s' % (tag[:-close], attr, want, tag[-close:])
        else:
            indent = re.match(r"[ \t]*", tag[nl + 1:]).group(0)
            tag = tag[:nl] + '\n%s%s="%s"' % (indent, attr, want) + tag[nl:]
    return tag, changed


def fix_formmetadata(data, form_path):
    """Insert the FormMetadata step as the first step, or bring an existing one to the canonical
    shape and the first position. Returns (form, change description or None)."""
    items = rootpanel_items(parse_spans(data))
    if items is None:
        raise FixError("form has no root panel items")
    steps = [c for c in items.children if c.tag not in ("layout", "cq:responsive")]
    at_step = [(i, c) for i, c in enumerate(steps) if is_formmetadata(c)]

    if at_step:
        index, node = at_step[0]
        if not formmetadata_deviations(node) and index == 0:
            return data, None
        open_end = open_tag_end(data, node.start)
        new_tag, changed = normalise_formmetadata_tag(data[node.start:open_end].decode("utf-8"))
        subtree = new_tag.encode("utf-8") + data[open_end:node.end]
        what = []
        if index == 0:
            out = data[:node.start] + subtree + data[node.end:]
        else:
            # Cut the subtree with its leading whitespace and line break, and put it right
            # after the root items' open tag, at the indentation of the current first step.
            cut = node.start
            while cut > 0 and data[cut - 1:cut] in (b" ", b"\t"):
                cut -= 1
            if cut > 0 and data[cut - 1:cut] == b"\n":
                cut -= 1
            items_end = open_tag_end(data, items.start)
            indent = line_indent(data, steps[0].start)
            out = data[:cut] + data[node.end:]
            out = out[:items_end] + ("\n" + indent).encode("utf-8") + subtree + out[items_end:]
            what.append("moved to first step")
        if changed:
            what.append("set " + ", ".join(sorted(changed)))
        return out, "FormMetadata step: " + "; ".join(what)

    if any(is_formmetadata(n) for s in steps for n in walk(s) if n is not s):
        raise FixError("FormMetadata fragment is nested inside a panel instead of being a "
                       "step; move it in AEM")
    items_end = open_tag_end(data, items.start)
    if data[items_end - 2:items_end] == b"/>":
        raise FixError("root panel items has no steps")
    indent = line_indent(data, steps[0].start) if steps else line_indent(data, items.start) + "    "
    # Deterministic node name, so a re-run produces the same node.
    tag = "fragment_" + str(uuid.uuid5(uuid.NAMESPACE_URL, "%s#formmetadata" % form_path))
    block = ("\n" + indent + formmetadata_xml(tag, indent)).encode("utf-8")
    return data[:items_end] + block + data[items_end:], "inserted FormMetadata step"


# =========================================================================== DAM metadata

METADATA_TAG = re.compile(rb'<metadata\b(?:\s+[\w:.\-]+="[^"]*")*\s*/?>', re.S)
ATTR_SPAN = re.compile(rb'(\s+)([\w:.\-]+)="([^"]*)"')


def set_attr(tag, name, value):
    """Open tag with `name="value"`. A missing attribute goes in FileVault's alphabetical
    position among the un-prefixed attributes, with the separator of its neighbour (so on its
    own line in a multi-line tag). Returns (tag, changed)."""
    bname, bval = name.encode(), value.encode()
    cur = re.search(rb'(?<=\s)' + re.escape(bname) + rb'="([^"]*)"', tag)
    if cur:
        if cur.group(1) == bval:
            return tag, False
        return tag[:cur.start(1)] + bval + tag[cur.end(1):], True
    spans = list(ATTR_SPAN.finditer(tag))
    after = next((m for m in spans if b":" not in m.group(2)
                  and m.group(2).lower() > bname.lower()), None)
    if after is not None:
        at, sep = after.start(), after.group(1)
    else:
        at, sep = spans[-1].end(), spans[-1].group(1)
    return tag[:at] + sep + bname + b'="' + bval + b'"' + tag[at:], True


def fix_metadata(dam, wants):
    """Apply [(attr, fn(current) -> wanted or None)] to the DAM <metadata> open tag.
    Returns (new_dam, [change descriptions])."""
    m = METADATA_TAG.search(dam)
    if not m:
        raise FixError("DAM asset has no <metadata> node")
    tag, changes = m.group(0), []
    for name, wanted_for in wants:
        cur = re.search(rb'(?<=\s)' + re.escape(name.encode()) + rb'="([^"]*)"', tag)
        current = cur.group(1).decode() if cur else None
        wanted = wanted_for(current)
        if wanted is None:
            continue
        tag, changed = set_attr(tag, name, wanted)
        if changed:
            changes.append('%s: %s -> "%s"' % (name, '"%s"' % current if current is not None
                                                else "not set", wanted))
    return dam[:m.start()] + tag + dam[m.end():], changes


def with_save_option(current):
    options = multi_value(current)
    if SAVE_OPTION in options:
        return None
    return "[" + ",".join(options + [SAVE_OPTION]) + "]"


def set_true(current):
    return None if current == "true" else "true"


# =========================================================================== package

def read_entries(zip_path):
    """(dam_path, dam_bytes, form_path, form_bytes) of one package."""
    with zipfile.ZipFile(zip_path) as z:
        dam = next((n for n in z.namelist() if n.startswith(DAM_ROOT)
                    and n.endswith("/.content.xml") and b"dorType" in z.read(n)), None)
        form = next((n for n in z.namelist() if n.startswith(FORM_ROOT)
                     and n.endswith("/.content.xml") and b'"cq:Page"' in z.read(n)), None)
        if dam is None or form is None:
            raise FixError("not an adaptive form package (DAM asset or form page missing)")
        return dam, z.read(dam), form, z.read(form)


def fix(dam, form, form_path, locales):
    """Pure transform of the two package entries. Returns (dam, form, [changes])."""
    changes = []
    if not has_summary(parse_prefixed(form)):
        form, dam, actions = fix_summary(form, dam, locales)
        changes.append("summary step built (%s)" % ", ".join(actions))

    form, step_change = fix_formmetadata(form, form_path)
    if step_change:
        changes.append(step_change)

    wants = [("redactoSummary", set_true), ("menuOptions", with_save_option)]
    if is_germany(form_path):
        wants.append(("printPartialMode", set_true))
    dam, meta_changes = fix_metadata(dam, wants)
    return dam, form, changes + meta_changes


def write_copy(src_zip, dst_zip, replace):
    """Copy of `src_zip` with the entries in `replace` swapped; every other entry byte for
    byte, in the same order."""
    with zipfile.ZipFile(src_zip) as src, zipfile.ZipFile(dst_zip, "w") as dst:
        for info in src.infolist():
            data = replace.get(info.filename) or src.read(info.filename)
            out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            out.compress_type = info.compress_type
            out.external_attr = info.external_attr
            out.create_system = info.create_system
            dst.writestr(out, data)


def process(zip_path, out_dir):
    """One report row: {package, status, features, changes, written, error}. Feature values:
    "ok" (already there), "fixed", "n/a", or "FAILED"."""
    row = {"package": zip_path, "status": None, "features": {}, "changes": [],
           "written": None, "error": None}
    scratch = tempfile.mkdtemp(prefix="form_features_")
    try:
        dam_path, dam, form_path, form = read_entries(zip_path)
        before = feature_states(dam, form, form_path)
        new_dam, new_form, changes = fix(dam, form, form_path, dictionary_locales(zip_path))
        row["changes"] = changes

        # Check the copy as written, not the bytes in memory.
        tmp = os.path.join(scratch, os.path.basename(zip_path))
        write_copy(zip_path, tmp, {dam_path: new_dam, form_path: new_form})
        _, dam_after, _, form_after = read_entries(tmp)
        after = feature_states(dam_after, form_after, form_path)

        for key, _label in FEATURES:
            if after[key] is None:
                row["features"][key] = "n/a"
            elif not after[key]:
                row["features"][key] = "FAILED"
            else:
                row["features"][key] = "ok" if before[key] else "fixed"
        failed = [k for k, v in row["features"].items() if v == "FAILED"]
        if failed:
            raise FixError("still missing after fixing: %s" % ", ".join(failed))

        if changes and out_dir:
            dst = os.path.join(out_dir, os.path.basename(zip_path))
            shutil.move(tmp, dst)
            row["written"] = dst
        row["status"] = "fixed" if changes else "ok"
    except (zipfile.BadZipFile, OSError, ValueError, ET.ParseError, expat.ExpatError,
            FixError) as exc:
        row.update(status="error", error=str(exc))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return row


# =========================================================================== report

def print_report(rows, dry_run, verbose):
    headers = ["Package"] + [label for _, label in FEATURES] + ["Result"]
    lines = []
    for r in rows:
        cells = [r["features"].get(k, "-") for k, _ in FEATURES]
        lines.append([os.path.basename(r["package"])] + cells + [r["status"].upper()])
    widths = [max(len(h), *(len(l[i]) for l in lines)) for i, h in enumerate(headers)]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    print(fmt % tuple(headers))
    print(fmt % tuple("-" * w for w in widths))
    for l in lines:
        print((fmt % tuple(l)).rstrip())

    errors = [r for r in rows if r["error"]]
    if errors:
        print("\nErrors:")
        for r in errors:
            print("  %s: %s" % (os.path.basename(r["package"]), r["error"]))
    if verbose:
        print("\nChanges:")
        for r in rows:
            for c in r["changes"]:
                print("  %s: %s" % (os.path.basename(r["package"]), c))

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n%d packages: %s%s" % (len(rows), ", ".join(
        "%d %s" % (v, k) for k, v in sorted(counts.items())),
        " (dry run, nothing written)" if dry_run else ""))


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["package", "result"] + [k for k, _ in FEATURES]
                   + ["changes", "written", "error"])
        for r in rows:
            w.writerow([os.path.basename(r["package"]), r["status"]]
                       + [r["features"].get(k, "") for k, _ in FEATURES]
                       + ["; ".join(r["changes"]), r["written"] or "", r["error"] or ""])


# =========================================================================== CLI

def collect(paths):
    found = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                found += [os.path.join(dirpath, f) for f in files if f.lower().endswith(".zip")]
        elif os.path.isfile(p):
            found.append(p)
        else:
            print("not a file or folder: %s" % p, file=sys.stderr)
            sys.exit(2)
    return sorted(found)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("paths", nargs="+", help="package ZIP(s) or folder(s) containing them")
    ap.add_argument("--out", help="folder for the fixed copies (created if missing)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--csv", metavar="FILE", help="also write the report to a CSV file")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every change made")
    args = ap.parse_args(argv)
    if bool(args.out) == args.dry_run:
        print("give exactly one of --out FOLDER or --dry-run", file=sys.stderr)
        return 2

    zips = collect(args.paths)
    if not zips:
        print("no .zip packages found", file=sys.stderr)
        return 2
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        out_real = os.path.realpath(args.out)
        if any(os.path.dirname(os.path.realpath(z)) == out_real for z in zips):
            print("--out must not be the folder the input packages are in", file=sys.stderr)
            return 2

    rows = [process(z, None if args.dry_run else args.out) for z in zips]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print_report(rows, args.dry_run, args.verbose)
    if args.csv:
        write_csv(rows, args.csv)
    return 0 if all(r["status"] in ("ok", "fixed") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
