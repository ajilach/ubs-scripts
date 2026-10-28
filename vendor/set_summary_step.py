"""
Fixer for PROBLEM-summary-step-redacto.

Brings one deployed package to the "Redacto renders the DoR from a populated summary"
shape (see summary_step.py for the three parts and why they are one state):

  1. `<summarypanel name="summaryPanel">` inserted as the step right before the preview
     step, holding `messagebox_ElsigCheck`, the `summaryComponent`, `submitErrorMessage`,
     and the form's existing `doroptionsubs` + metadata controls, which are **moved**
     (never copied or synthesised — the metadata control carries per-form data).
  2. `setSummaryData(guideRootPanel)` prepended to the toolbar `nextitemnav` `fd:click`
     array, ahead of whatever the form already runs there.
  3. `redactoSummary="true"` on the DAM asset's metadata node.

Every part is applied only when missing, so the script is idempotent and a partially
correct form (e.g. panel present, hook absent) gets only what it lacks.

The synthesised block copies the **deployed** reference form AAAV_019, not the raw engine
template: that is the post-sweep ground truth, which differs from the engine in carrying
`summaryExclusion="true"` next to `dorExclusion="true"` (PROBLEM-dor-exclusion-implies-
summary). Indentation is taken from the form's own preview step so the moved nodes land at
the column they already use and the corpus's two formatting lineages both stay tidy.

Byte-offset splices (expat spans) preserve the rest of the file.

Usage:  python3 .claude/scripts/set_summary_step.py <zip> [--stage _edit]
Output (stdout): JSON { inner, dam, actions, moved, ok, flag? }
"""
import argparse
import json
import os
import re
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import summary_step as S

CREATED = "{Date}2025-01-01T00:00:00.000Z"

MESSAGEBOX_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/messagebox"
GRID_LAYOUT = "fd/af/layouts/gridFluidLayout2"

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

# One SCRIPTMODEL entry for the fd:click array, in FileVault's on-disk escaping: `"`
# written as &quot; and every JSON comma escaped as `\,`. Built by substitution rather
# than json.dumps because the value must match the corpus byte for byte.
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


# --------------------------------------------------------------------------- rendering

def attr_indent(data, node, fallback):
    """The column the node's attributes are written at, or `fallback` when the open tag
    is a single line. Read from the form itself: the corpus has two formatting lineages."""
    nl = data.find(b"\n", node.start, node.open_end)
    if nl < 0:
        return fallback
    i = nl + 1
    while i < len(data) and data[i] in b" \t":
        i += 1
    return data[nl + 1:i].decode()


def render(tag, attrs, indent, attr_ind, close):
    """One element, attributes one per line (the corpus style). `close` is '/>' for a
    self-closing element or '>' for one that gets children."""
    lines = [f"{indent}<{tag}"]
    lines += [f'{attr_ind}{k}="{v}"' for k, v in attrs]
    lines[-1] += close
    return "\n".join(lines)


def reindent(text, delta):
    """Shift every line's leading whitespace by `delta` columns. Only lines that begin
    with whitespace followed by `<` or an `attr="` token are touched, so a rich-text
    attribute value spanning lines is left alone."""
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


def build_panel(data, anchor, moved_nodes, author, title=None):
    """The whole `<summarypanel>` element as text, indented to match `anchor`."""
    title = title or S.PANEL_ATTRS["jcr:title"]
    base = S.indent_of(data, anchor.start)
    a_ind = attr_indent(data, anchor, base + "    ")
    # Take the child indents from the anchor's own items/children when it has them, so
    # the moved nodes need no reindent at all on the common shape.
    items = next((c for c in anchor.children if c.tag == "items"), None)
    items_ind = S.indent_of(data, items.start) if items is not None else base + "    "
    items_attr = attr_indent(data, items, items_ind + "    ") if items is not None \
        else items_ind + "    "
    kids = [c for c in items.children] if items is not None else []
    child_ind = S.indent_of(data, kids[0].start) if kids else items_ind + "    "
    child_attr = attr_indent(data, kids[0], child_ind + "    ") if kids \
        else child_ind + "    "

    panel_attrs = [
        ("jcr:created", CREATED),
        ("jcr:createdBy", author),
        ("jcr:lastModified", CREATED),
        ("jcr:lastModifiedBy", author),
        ("jcr:primaryType", "nt:unstructured"),
        ("jcr:title", title),
        ("sling:resourceType", S.PANEL_RT),
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
        ("sling:resourceType", S.SUMMARY_RT),
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
        own = S.indent_of(data, node.start)
        parts.append(child_ind + reindent(text, len(child_ind) - len(own)))
    parts.append(f"{items_ind}</items>")
    parts.append(render("layout", LAYOUT_ATTRS, items_ind, items_attr, "/>"))
    parts.append(f"{base}</summarypanel>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- edits

DOROPTS_ATTRS = [
    ("jcr:primaryType", "nt:unstructured"),
    ("sling:resourceType", S.DOROPTS_RT),
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


def open_tag_span(data, node):
    return node.start, S.open_tag_end(data, node.start)


def rewrite_open_tag(data, node, wanted, close):
    """The node's open tag with `wanted` applied: values replaced where they differ, missing
    attributes inserted in FileVault's alphabetical position. Audit attributes are kept."""
    start, end = open_tag_span(data, node)
    attrs = dict(re.findall(r'([\w:.\-]+)="([^"]*)"', data[start:end].decode("utf-8")))
    attrs.update(wanted)
    ordered = sorted(attrs.items(), key=lambda kv: (not kv[0].startswith("jcr:"),
                                                    not kv[0].startswith("sling:"), kv[0]))
    indent = S.indent_of(data, node.start)
    a_ind = attr_indent(data, node, indent + "    ")
    return start, end, render(node.tag, ordered, "", a_ind, close).lstrip("\n").encode()


def normalise_panel(data, panel, st, doc, items):
    """(edits, actions, flags) bringing an existing summary panel to the reference shape."""
    edits, actions, flags = [], [], []

    if st["panel_attrs"]:
        close = ">" if panel.children else "/>"
        # The heading is the one the state worked out — the master, or the form's own words where it
        # ships no dictionary — not the reference's.
        wanted = dict(S.PANEL_ATTRS)
        wanted["jcr:title"] = next((w for a, _c, w in st["panel_attrs"] if a == "jcr:title"),
                                   panel.attrs.get("jcr:title") or S.PANEL_ATTRS["jcr:title"])
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
            if (node.attrs.get("formrange_code")
                    and twin is not None
                    and node.attrs.get("formrange_code") != twin.attrs.get("formrange_code")):
                flags.append("two %s controls with different form codes" % wanted)
                continue
            start = node.start - len(S.indent_of(data, node.start))
            end = node.end + (1 if data[node.end:node.end + 1] == b"\n" else 0)
            edits.append((start, "cut", end))
            actions.append("dropped-duplicate:" + wanted)
            continue
        if kind == "rename":
            node = next((c for c in kids if c.attrs.get("name") == current), None)
            if node is None:
                continue
            start, end = open_tag_span(data, node)
            tag = data[start:end].decode("utf-8")
            new = re.sub(r'(?<![\w:.\-])name="[^"]*"', 'name="%s"' % wanted, tag, count=1)
            edits.append((start, "rep", (end, new.encode())))
            actions.append("rename:" + wanted)
            continue
        # missing: move the form's own control in, or synthesise the ones that carry no data
        rt = {"doroptionsubs": S.DOROPTS_RT, "metadataTextDraw": S.METADATA_RT}.get(wanted)
        outside = [n for n in S.walk(items)
                   if n.attrs.get("sling:resourceType") == rt and not _inside(panel, n)] if rt else []
        # The preview step is on its way out (PROBLEM-preview-step-removed), so a copy sitting in it
        # is never the one to keep while another exists elsewhere.
        if len(outside) > 1:
            outside = [n for n in outside if not _in_preview(items, n)] or outside
        anchor = kids[-1] if kids else None
        if outside:
            if len(outside) > 1:
                flags.append("%d %s controls outside the panel" % (len(outside), wanted))
                continue
            node = outside[0]
            text = data[node.start:node.end].decode("utf-8", "replace")
            own = S.indent_of(data, node.start)
            child_ind = S.indent_of(data, anchor.start) if anchor is not None else own
            cut_from = node.start - len(own)
            cut_to = node.end + (1 if data[node.end:node.end + 1] == b"\n" else 0)
            edits.append((cut_from, "cut", cut_to))
            moved = child_ind + reindent(text, len(child_ind) - len(own))
            moved = re.sub(r'(?<![\w:.\-])name="[^"]*"', 'name="%s"' % wanted, moved, count=1)
            at = kids_node.end - len("</items>") - len(S.indent_of(data, kids_node.end))
            edits.append((at, "ins", (moved + "\n").encode()))
            actions.append("moved:" + wanted)
        elif wanted in SYNTHESISED and kids_node is not None:
            child_ind = S.indent_of(data, anchor.start) if anchor is not None else "    "
            child_attr = attr_indent(data, anchor, child_ind + "    ") if anchor is not None \
                else child_ind + "    "
            block = render(TAG_FOR[wanted], SYNTHESISED[wanted], child_ind, child_attr, "/>")
            at = kids_node.end - len("</items>") - len(S.indent_of(data, kids_node.end))
            edits.append((at, "ins", (block.lstrip("\n") + "\n").encode()))
            actions.append("added:" + wanted)
        else:
            flags.append("no %s control to move in" % wanted)
    return edits, actions, flags


def _inside(panel, node):
    return any(n is node for n in S.walk(panel))


def _in_preview(items, node):
    steps = [c for c in items.children if (c.attrs.get("name") or "").lower() in ("preview", "pn_preview")]
    return any(_inside(step, node) for step in steps)


def dam_insert(ddata):
    """(pos, text) inserting `redactoSummary="true"` into the DAM metadata node, in the
    alphabetical position FileVault writes attributes in, or None when already set."""
    if b'redactoSummary="true"' in ddata:
        return None
    text = ddata.decode()
    m = re.search(r'<metadata\b', text)
    if not m:
        return None
    end = S.open_tag_end(ddata, m.start())
    tag = text[m.start():end]
    attrs = list(re.finditer(r'\n(\s+)([\w:.\-]+)="', tag))
    if not attrs:
        return None
    ind = attrs[-1].group(1)
    for am in attrs:
        # Namespaced attributes sort ahead of the plain ones in FileVault's output.
        if ":" not in am.group(2) and am.group(2) > "redactoSummary":
            return (m.start() + am.start(), f'\n{ind}redactoSummary="true"')
    # Nothing sorts after it — append as the tag's last attribute.
    close = end - 2 if text[end - 2:end] == "/>" else end - 1
    return (close, f'\n{ind}redactoSummary="true"')


def click_prepend(data, holder):
    """(pos, text) prepending the setSummaryData SCRIPTMODEL to the holder's fd:click
    array, or None when the hook is already there / there is no array to prepend to."""
    seg = data[holder.start:holder.open_end].decode("utf-8", "replace")
    m = re.search(r'fd:click="\[', seg)
    if not m or "setSummaryData" in seg:
        return None
    fm = FIELD_RE.search(seg)
    entry = CLICK_ENTRY.replace("{FIELD}", fm.group(1) if fm else DEFAULT_FIELD) \
                       .replace("{CONTENT}", S.SET_SUMMARY_DATA)
    return (holder.start + m.end(), entry + ",")


def apply(data, ddata, locales=()):
    """(form_bytes, dam_bytes, report) — the whole transform as a pure function of the
    two members' bytes, so it is unit-testable and the CLI only does zip I/O.

    `report` carries `actions` (which of panel/hook/redacto were applied), `moved` (the
    names of the relocated controls) and `ok`, which is re-derived from the OUTPUT with
    the detector's own logic rather than from what this function believes it did."""
    def bail(flag, actions=(), moved=()):
        return data, ddata, {"actions": list(actions), "moved": list(moved),
                             "ok": False, "flag": flag}

    doc = S.parse_spans(data)
    items = S.rootpanel_items(doc)
    if items is None:
        return bail("no root panel items")

    actions, moved, edits = [], [], []   # edits: (pos, kind, ...) applied right-to-left

    if S.summary_panel(items) is None:
        anchor = S.preview_step(items)
        if anchor is None:
            return bail("no preview step to anchor the summary step to")
        take = []
        for rt in (S.DOROPTS_RT, S.METADATA_RT):
            found = [n for n in S.walk(items) if n.attrs.get("sling:resourceType") == rt]
            if len(found) != 1:
                return bail(f"expected exactly 1 {rt.rsplit('/', 1)[-1]} control, "
                            f"found {len(found)}")
            take.append(found[0])
        moved_nodes = [(n, data[n.start:n.end].decode("utf-8", "replace")) for n in take]
        moved = [n.attrs.get("name", "") for n in take]
        author = anchor.attrs.get("jcr:createdBy", "blueprint")
        block = build_panel(data, anchor, moved_nodes, author, S.wanted_title(data, locales))
        for n in take:                    # cut each moved node together with its own line
            start = n.start - len(S.indent_of(data, n.start))
            end = n.end + (1 if data[n.end:n.end + 1] == b"\n" else 0)
            edits.append((start, "cut", end))
        indent = S.indent_of(data, anchor.start)
        edits.append((anchor.start - len(indent), "ins", (block + "\n").encode()))
        actions.append("panel")

    panel = S.summary_panel(items)
    if panel is not None:
        st_now = S.state_from_bytes(data, ddata, locales)
        p_edits, p_actions, p_flags = normalise_panel(data, panel, st_now, doc, items)
        if p_flags:
            return bail("; ".join(p_flags), actions, moved)
        edits.extend(p_edits)
        actions.extend(p_actions)

    # Redacto renders the preview its own way, so the old toolbar button goes with its rules.
    for button in S.preview_buttons(doc):
        start = button.start - len(S.indent_of(data, button.start))
        end = button.end + (1 if data[button.end:button.end + 1] == b"\n" else 0)
        edits.append((start, "cut", end))
        actions.append("preview-button")

    holder = S.nav_click_holder(S.nextitemnav(doc))
    if holder is None:
        return bail("no nextitemnav fd:click to hook", actions, moved)
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
    di = dam_insert(ddata)
    if di:
        dout = ddata[:di[0]] + di[1].encode() + ddata[di[0]:]
        actions.append("redacto")

    st = S.state_from_bytes(out, dout, locales)
    report = {"actions": actions, "moved": moved, "ok": S.is_complete(st)}
    if not report["ok"]:
        missing = [k for k in ("redacto", "panel", "summary_component", "hook") if not st[k]]
        if st["panel_attrs"]:
            missing.append("panel-attrs:" + ",".join(a for a, _c, _w in st["panel_attrs"]))
        if st["panel_children"]:
            missing.append("panel-children:" + ",".join(w for _k, _c, w in st["panel_children"]))
        if st["preview_buttons"]:
            missing.append("preview-buttons:%d" % st["preview_buttons"])
        report["flag"] = "post-fix state incomplete: " + ",".join(missing)
    return out, dout, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--stage", default="_edit")
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip) as z:
        inner, data = S.form_entry(z)
        dam, ddata = S.dam_entry(z)
    if inner is None or dam is None:
        print(json.dumps({"ok": False, "flag": "missing form or DAM .content.xml"}))
        sys.exit(1)

    out, dout, report = apply(data, ddata, S.dictionary_locales(args.zip))
    report = dict(report, inner=inner, dam=dam)

    if report["actions"] and report["ok"]:
        members = []
        for name, blob in ((inner, out), (dam, dout)):
            dest = os.path.join(args.stage, name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(blob)
            members.append(name)
        r = subprocess.run(["zip", "-q", os.path.abspath(args.zip)] + members,
                           cwd=args.stage, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            sys.exit(1)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
