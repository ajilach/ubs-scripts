"""
Shared logic for PROBLEM-formmetadata-step: the FormMetadata fragment as a form's first wizard step.

Imported by `find_formmetadata_step.py` (report) and `set_formmetadata_step.py` (fix) so both agree
on what the canonical node is.

The FormMetadata step is a node carrying `fragRef == FRAG_PATH` that is a **direct child** of
`guideContainer > rootPanel(guideRootPanel) > items` — a step, not a nested panel. It is identified
by `fragRef`, never by name. `CANONICAL` is the attribute set it must carry (reference: AAOV_033);
`deviations()` says which of those a node gets wrong. Attributes and children outside that set are
left alone by the fixer and reported by the detector.

Element offsets come from expat (`start`, `end_start`), so the fixer can rewrite one open tag or cut
a whole subtree out without regex-parsing the document. `tag_span()` is quote-aware: rich-text
attribute values contain a literal '>'.
"""
import zipfile

from xml.parsers import expat

FRAG_PATH = "/content/dam/formsanddocuments/afforms_global_fragmentlib/formmetadata"
SPECIAL_NAMES = {"formmetadata", "formMetadata", "FormMetadata", "autoSaveInfo", "signerInfo",
                 "doroptionsubs", "PN_Preview"}


def form_entry_and_xml(zip_path):
    """(entry_name, xml_text) of the form's cq:Page .content.xml, or (None, None)."""
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


def form_xml(zip_path):
    return form_entry_and_xml(zip_path)[1]


CANONICAL = {
    "jcr:primaryType": "nt:unstructured",
    "jcr:title": "FormMetadata",
    # The FormMetadata node is a fragRef guidePanel, so PROBLEM-panel-type-ubs (#20/#137)
    # covers it: every panel is the UBS custom panel, fragments included. This flipped from
    # "fd/af/components/panel" together with that sweep's rollout — the two are only
    # consistent when both land at once, so do not change one without the other.
    "sling:resourceType": "ajila-forms-customers/ajila-forms-ubs/components/controls/panel",
    "dorExcludeDescription": "true",
    "dorExcludeTitle": "true",
    "dorExclusion": "true",
    "fragRef": FRAG_PATH,
    "guideNodeClass": "guidePanel",
    "name": "FormMetadata",
    "textIsRich": "true",
    "visible": "false",
}


class N:
    __slots__ = ("tag", "attrs", "children", "parent", "start", "end_start")

    def __init__(s, tag, attrs, parent, start):
        s.tag, s.attrs, s.children, s.parent, s.start = tag, attrs, [], parent, start
        s.end_start = None      # offset of the closing tag, if the element has one


def parse(xml):
    """Parse into an N tree. `start` is the element's byte offset in xml.encode('utf-8');
    `end_start` is the offset of its closing tag (== `start` when self-closed)."""
    root = N("__root__", {}, None, -1)
    stack = [root]
    p = expat.ParserCreate()

    def start(name, a):
        node = N(name, a, stack[-1], p.CurrentByteIndex)
        stack[-1].children.append(node)
        stack.append(node)

    def end(name):
        stack[-1].end_start = p.CurrentByteIndex
        stack.pop()

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.Parse(xml.encode("utf-8"), True)
    return root


def node_span(data, node):
    """(start, end) byte offsets of the element **including** its closing tag."""
    open_start, open_end = tag_span(data, node.start)
    if node.end_start is None or node.end_start <= node.start:
        return open_start, open_end                     # self-closed: <tag …/>
    return open_start, node.end_start + len(f"</{node.tag}>".encode("utf-8"))


def deviations(node):
    """{attr: current_value_or_None} for every canonical attribute this node gets wrong."""
    return {k: node.attrs.get(k) for k, v in CANONICAL.items() if node.attrs.get(k) != v}


def extra_attrs(node):
    """Attributes beyond the canonical set, excluding AEM bookkeeping. Kept by the fixer."""
    return {k: v for k, v in node.attrs.items()
            if k not in CANONICAL and not k.startswith(("jcr:created", "jcr:lastModified"))}


def extra_children(node):
    """Children beyond the expected `layout` + `items`. Kept by the fixer, flagged for a human."""
    return [c.tag for c in node.children if c.tag not in ("layout", "items")]


def tag_span(data, start):
    """(start, end) byte offsets of the open tag at `start`, quote-aware: a '>' inside a
    double-quoted attribute value (rich-text `_value="&lt;p>…"`) does not end the tag."""
    i, in_q = start, False
    while i < len(data):
        ch = data[i:i + 1]
        if ch == b'"':
            in_q = not in_q
        elif ch == b">" and not in_q:
            return start, i + 1
        i += 1
    raise ValueError(f"unterminated open tag at byte {start}")


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
    if not gc:
        return None
    rp = (find(gc, lambda n: n.attrs.get("name") == "guideRootPanel")
          or find(gc, lambda n: n.tag == "rootPanel"))
    if not rp:
        return None
    for c in rp.children:
        if c.tag == "items":
            return c
    return None


def is_formmetadata_fragment(node):
    return (node.attrs.get("fragRef") or "").rstrip("/") == FRAG_PATH


def label(node):
    return node.attrs.get("name") or node.tag


def inspect(xml):
    """{items, present, node, index, steps, nested, duplicates}.

    `present` is True only for a fragment at step level; one nested deeper is returned under
    `nested` so it is neither double-inserted nor auto-moved."""
    items = rootpanel_items(parse(xml))
    if items is None:
        return {"items": None, "present": False, "node": None, "index": None,
                "steps": [], "nested": []}
    steps = [c for c in items.children if c.tag not in ("layout", "cq:responsive")]
    at_step = [(i, c) for i, c in enumerate(steps) if is_formmetadata_fragment(c)]
    nested = []

    def walk(n, top=True):
        for c in n.children:
            if not top and is_formmetadata_fragment(c):
                nested.append(c)
            walk(c, False)

    for s in steps:
        walk(s, False)
    idx, node = (at_step[0] if at_step else (None, None))
    return {"items": items, "present": node is not None, "node": node, "index": idx,
            "steps": steps, "nested": nested, "duplicates": at_step[1:]}
