#!/usr/bin/env python3
"""
Enable the agreed features on UBS AEM Adaptive Form packages: the fixing counterpart of
check_form_features.py.

  save_as_draft  DAM metadata `menuOptions` gets `ajila-forms-ubs-menu-option-save`
  print_partial  DAM metadata `printPartialMode="true"`, on Germany forms only
  input_fields   the FormMetadata fragment is inserted as the form's first wizard step
                 (canonical shape, as PROBLEM-formmetadata-step in
                 ajila-forms-conversion-feedback)
  redacto_dor    DAM metadata `redactoSummary="true"`, only when the form already has its
                 summary step: Redacto builds the DoR from the summary, so the flag without
                 the step gives an empty DoR

Not done by this script, reported under "review" instead:
  - adding a missing summary step (and with it the Redacto flag): use
    set_summary_step.py in ajila-forms-conversion-feedback,
  - a FormMetadata fragment nested inside another panel instead of being a step.

Only the affected attributes and the inserted step change; the rest of each file is kept
byte for byte. Every fixed package is re-checked with check_form_features.py before it is
written. Fixed packages go to --out as copies; the input ZIPs are never modified.
Python 3.8+, standard library only; check_form_features.py must sit next to this file.

Usage:
    python3 fix_form_features.py PACKAGE.zip [...] --out FIXED_DIR
    python3 fix_form_features.py FOLDER --out FIXED_DIR
    python3 fix_form_features.py FOLDER --dry-run          # report only, write nothing

Exit code: 0 if every package ends with every check passing, 1 if any package needs
review or could not be processed, 2 on bad usage.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
from xml.parsers import expat

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_form_features as check  # noqa: E402

METADATA_TAG = re.compile(rb'<metadata\b(?:\s+[\w:.\-]+="[^"]*")*\s*/?>', re.S)
ATTR_SPAN = re.compile(rb'(\s+)([\w:.\-]+)="([^"]*)"')

# --------------------------------------------------------------------------- byte edits


def set_attr(tag, name, value):
    """Open tag with `name="value"`. A missing attribute goes in FileVault's alphabetical
    position among the un-prefixed attributes, with the separator of its neighbour (so on
    its own line in a multi-line tag). Returns (tag, changed)."""
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


def tag_span(data, start):
    """(start, end) of the open tag at `start`, quote-aware: rich-text values hold '>'."""
    in_quote = False
    for i in range(start, len(data)):
        ch = data[i]
        if ch == 0x22:
            in_quote = not in_quote
        elif ch == 0x3E and not in_quote:
            return start, i + 1
    raise ValueError("unterminated open tag at byte %d" % start)


def indent_of(data, offset):
    line_start = data.rfind(b"\n", 0, offset) + 1
    return re.match(rb"[ \t]*", data[line_start:offset]).group(0).decode()


# --------------------------------------------------------------------------- DAM metadata

def fix_metadata(dam, wants):
    """Apply [(attr, fn(current) -> wanted or None)] to the DAM <metadata> open tag.
    Returns (new_dam, [change descriptions])."""
    m = METADATA_TAG.search(dam)
    if not m:
        raise ValueError("DAM asset has no <metadata> node")
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
    options = check.multi_value(current)
    if check.SAVE_OPTION in options:
        return None
    return "[" + ",".join(options + [check.SAVE_OPTION]) + "]"


# --------------------------------------------------------------------------- FormMetadata step

class Node:
    __slots__ = ("tag", "attrs", "children", "start")

    def __init__(self, tag, attrs, start):
        self.tag, self.attrs, self.start, self.children = tag, attrs, start, []


def parse(data):
    root = Node("__root__", {}, -1)
    stack = [root]
    p = expat.ParserCreate()

    def start(name, attrs):
        node = Node(name, attrs, p.CurrentByteIndex)
        stack[-1].children.append(node)
        stack.append(node)

    p.StartElementHandler = start
    p.EndElementHandler = lambda _name: stack.pop()
    p.Parse(data, True)
    return root


def walk(node):
    yield node
    for c in node.children:
        yield from walk(c)


def root_items(root):
    panel = next((n for n in walk(root) if n.attrs.get("name") == "guideRootPanel"
                  or n.tag == "rootPanel"), None)
    return next((c for c in panel.children if c.tag == "items"), None) if panel else None


def is_formmetadata(node):
    return node.attrs.get("fragRef", "").rstrip("/") == check.FORMMETADATA_FRAGMENT


def formmetadata_xml(tag, indent):
    """The canonical FormMetadata step (reference AAOV_033, as set_formmetadata_step.py)."""
    a, i2 = indent + "    ", indent + "    "
    return (
        '<%s\n' % tag
        + '%sjcr:primaryType="nt:unstructured"\n' % a
        + '%sjcr:title="FormMetadata"\n' % a
        + '%ssling:resourceType="ajila-forms-customers/ajila-forms-ubs/components/controls/panel"\n' % a
        + '%sdorExcludeDescription="true"\n' % a
        + '%sdorExcludeTitle="true"\n' % a
        + '%sdorExclusion="true"\n' % a
        + '%sfragRef="%s"\n' % (a, check.FORMMETADATA_FRAGMENT)
        + '%sguideNodeClass="guidePanel"\n' % a
        + '%sname="FormMetadata"\n' % a
        + '%stextIsRich="true"\n' % a
        + '%svisible="false">\n' % a
        + '%s<layout\n' % i2
        + '%s    jcr:primaryType="nt:unstructured"\n' % i2
        + '%s    sling:resourceType="fd/af/layouts/gridFluidLayout2"\n' % i2
        + '%s    columns="1"\n' % i2
        + '%s    dorLayoutType="columnar"\n' % i2
        + '%s    dorNumCols="1"\n' % i2
        + '%s    nonNavigable="{Boolean}true"\n' % i2
        + '%s    toolbarPosition="Bottom"/>\n' % i2
        + '%s<items\n' % i2
        + '%s    jcr:primaryType="nt:unstructured"\n' % i2
        + '%s    sling:resourceType="fd/af/layouts/gridFluidLayout2"/>\n' % i2
        + '%s</%s>' % (indent, tag)
    )


def insert_formmetadata(form, form_path):
    """(new_form, change or None, review or None)."""
    root = parse(form)
    items = root_items(root)
    if items is None:
        return form, None, "form has no root panel items"
    steps = [c for c in items.children if c.tag not in ("layout", "cq:responsive")]
    if any(is_formmetadata(s) for s in steps):
        return form, None, None
    if any(is_formmetadata(n) for s in steps for n in walk(s)):
        return form, None, ("FormMetadata fragment is nested inside a panel instead of "
                            "being a step; move it in AEM")
    _, items_end = tag_span(form, items.start)
    if form[items_end - 2:items_end] == b"/>":
        return form, None, "root panel items has no steps"
    indent = indent_of(form, steps[0].start) if steps else indent_of(form, items.start) + "    "
    # Deterministic node name, so a re-run produces the same node.
    tag = "fragment_" + str(uuid.uuid5(uuid.NAMESPACE_URL, form_path + "#formmetadata"))
    block = ("\n" + indent + formmetadata_xml(tag, indent)).encode("utf-8")
    return form[:items_end] + block + form[items_end:], "inserted FormMetadata step", None


# --------------------------------------------------------------------------- package

def entries(z):
    """(dam_path, form_path) inside the package."""
    dam = next((n for n in z.namelist() if n.startswith(check.DAM_ROOT)
                and n.endswith("/.content.xml") and b"dorType" in z.read(n)), None)
    form = next((n for n in z.namelist() if n.startswith(check.FORM_ROOT)
                 and n.endswith("/.content.xml") and b'"cq:Page"' in z.read(n)), None)
    if dam is None or form is None:
        raise ValueError("not an adaptive form package (DAM asset or form page missing)")
    return dam, form


def fix(dam, form, form_path):
    """Pure transform of the two package entries. Returns (dam, form, changes, review, left):
    `left` names the checks that are expected to still fail because of a review item."""
    changes, review, left = {}, [], set()
    root = check.parse_prefixed(form)
    has_summary = check.check_summary(root)[0]

    wants = [("menuOptions", with_save_option)]
    if check.is_germany(form_path):
        wants.append(("printPartialMode", lambda cur: None if cur == "true" else "true"))
    if has_summary:
        wants.append(("redactoSummary", lambda cur: None if cur == "true" else "true"))
    else:
        review.append("no summary step: Redacto DoR and summary not enabled; add the summary "
                      "step first (set_summary_step.py in ajila-forms-conversion-feedback)")
        left |= {"redacto_dor", "summary"}
    dam, meta_changes = fix_metadata(dam, wants)
    if meta_changes:
        changes["metadata"] = meta_changes

    form, step_change, step_review = insert_formmetadata(form, form_path)
    if step_change:
        changes["input_fields"] = [step_change]
    if step_review:
        review.append(step_review)
        left.add("input_fields")
    return dam, form, changes, review, left


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
    row = {"package": zip_path, "status": None, "changes": {}, "review": [],
           "written": None, "error": None}
    scratch = tempfile.mkdtemp(prefix="form_features_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            dam_path, form_path = entries(z)
            dam, form = z.read(dam_path), z.read(form_path)
        new_dam, new_form, changes, review, left = fix(dam, form, form_path)
        parse(new_dam), parse(new_form)                          # still well-formed
        row["changes"], row["review"] = changes, review

        tmp = os.path.join(scratch, os.path.basename(zip_path))
        write_copy(zip_path, tmp, {dam_path: new_dam, form_path: new_form})
        # Hard post-condition: the copy passes every check the script is meant to fix.
        result = check.check_package(tmp)
        failing = [k for k, c in result["checks"].items() if c["ok"] is False and k not in left]
        if result["error"] or failing:
            raise ValueError("fixed copy still fails: %s" % (result["error"] or ", ".join(failing)))

        if changes and out_dir:
            dst = os.path.join(out_dir, os.path.basename(zip_path))
            shutil.move(tmp, dst)
            row["written"] = dst
        row["status"] = "review" if review else ("fixed" if changes else "ok")
    except (zipfile.BadZipFile, OSError, ValueError, expat.ExpatError) as exc:
        row.update(status="error", error=str(exc))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return row


# --------------------------------------------------------------------------- CLI

def print_report(rows, dry_run):
    for r in rows:
        print("%s  [%s]" % (os.path.basename(r["package"]), r["status"].upper()))
        if r["error"]:
            print("    error: %s" % r["error"])
        for group, items in r["changes"].items():
            for item in items:
                print("    %-14s %s" % (group, item))
        for msg in r["review"]:
            print("    %-14s %s" % ("REVIEW", msg))
        if r["written"]:
            print("    written: %s" % r["written"])
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n%d packages: %s%s" % (len(rows), ", ".join(
        "%d %s" % (v, k) for k, v in sorted(counts.items())),
        " (dry run, nothing written)" if dry_run else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("paths", nargs="+", help="package ZIP(s) or folder(s) containing them")
    ap.add_argument("--out", help="folder for the fixed copies (created if missing)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)
    if bool(args.out) == args.dry_run:
        print("give exactly one of --out FOLDER or --dry-run", file=sys.stderr)
        return 2

    zips = check.collect(args.paths)
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
        print_report(rows, args.dry_run)
    return 0 if all(r["status"] in ("ok", "fixed") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
