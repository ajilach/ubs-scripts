"""
Fixer for PROBLEM-formmetadata-step: make the FormMetadata fragment the form's first step, in the
canonical shape.

  - missing        → insert the canonical node as the first child of the root `items`, indented to
                     match the existing first step. Its tag is `fragment_<uuid5>` of the form's JCR
                     path, so re-runs produce the same node.
  - present, wrong → set every canonical attribute on the existing open tag, and move the node to
                     the front if it is not there. Attributes and children outside the canonical set
                     are kept, and reported under `kept_extra_attrs` / `extra_children`.

No `jcr:created` / `jcr:lastModified` is written: AEM sets those itself.

Edits are byte-offset splices into the targeted tags only, so the rest of the file is untouched.

Usage (run_sweep fixer contract — zip as arg 1):
    python3 .claude/scripts/set_formmetadata_step.py <zip> [--dry-run]

Output (stdout): JSON {inner, inserted, normalised, moved, attrs, kept_extra_attrs, extra_children,
already_present, skipped, tag, well_formed, canonical_first, changed, ok}
`ok` = the result parses AND the fragment is the first step AND every canonical attribute matches.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import uuid

from formmetadata_step import (CANONICAL, FRAG_PATH, deviations, extra_attrs, extra_children,
                               form_entry_and_xml, inspect, node_span, parse, rootpanel_items,
                               tag_span)

NAME = "FormMetadata"


def normalise_tag(tag_bytes, node):
    """Set every canonical attribute to its canonical value on this open tag.

    Quote-aware: an existing value is replaced in place; a missing one is added on its own line, or
    inline before the closing `>` / `/>` when the tag is written on a single line. Attributes outside
    `CANONICAL` are left untouched. Returns (new_tag_bytes, {attr: (before, after)})."""
    tag = tag_bytes.decode("utf-8")
    changed = {}
    for attr, want in CANONICAL.items():
        cur = re.search(rf'(?<![\w:.\-]){re.escape(attr)}="([^"]*)"', tag)
        if cur:
            if cur.group(1) == want:
                continue
            changed[attr] = (cur.group(1), want)
            tag = tag[:cur.start()] + f'{attr}="{want}"' + tag[cur.end():]
            continue
        changed[attr] = (None, want)
        nl = tag.find("\n")
        if nl == -1:                                    # single-line open tag
            close = 2 if tag.rstrip().endswith("/>") else 1
            tag = f'{tag[:-close]} {attr}="{want}"{tag[-close:]}'
        else:                                           # one attribute per line
            indent = re.match(r"[ \t]*", tag[nl + 1:]).group(0)
            tag = tag[:nl] + f'\n{indent}{attr}="{want}"' + tag[nl:]
    return tag.encode("utf-8"), changed


def node_xml(tag, indent):
    """The canonical FormMetadata subtree, indented to sit at `indent`."""
    i, i2 = indent, indent + "    "
    a = indent + "    "
    return (
        f'<{tag}\n'
        f'{a}jcr:primaryType="nt:unstructured"\n'
        f'{a}jcr:title="{NAME}"\n'
        # UBS custom panel, per PROBLEM-panel-type-ubs (#20/#137) — see formmetadata_step.CANONICAL
        f'{a}sling:resourceType="ajila-forms-customers/ajila-forms-ubs/components/controls/panel"\n'
        f'{a}dorExcludeDescription="true"\n'
        f'{a}dorExcludeTitle="true"\n'
        f'{a}dorExclusion="true"\n'
        f'{a}fragRef="{FRAG_PATH}"\n'
        f'{a}guideNodeClass="guidePanel"\n'
        f'{a}name="{NAME}"\n'
        f'{a}textIsRich="true"\n'
        f'{a}visible="false">\n'
        f'{i2}<layout\n'
        f'{i2}    jcr:primaryType="nt:unstructured"\n'
        f'{i2}    sling:resourceType="fd/af/layouts/gridFluidLayout2"\n'
        f'{i2}    columns="1"\n'
        f'{i2}    dorLayoutType="columnar"\n'
        f'{i2}    dorNumCols="1"\n'
        f'{i2}    nonNavigable="{{Boolean}}true"\n'
        f'{i2}    toolbarPosition="Bottom"/>\n'
        f'{i2}<items\n'
        f'{i2}    jcr:primaryType="nt:unstructured"\n'
        f'{i2}    sling:resourceType="fd/af/layouts/gridFluidLayout2"/>\n'
        f'{i}</{tag}>'
    )


def deterministic_tag(entry):
    """`fragment_<uuid5>` of the form's ZIP entry path — stable across re-runs."""
    return "fragment_" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"{entry}#formmetadata"))


def indent_of(data, offset):
    """The indentation of the line containing `offset`."""
    line_start = data.rfind(b"\n", 0, offset) + 1
    return re.match(rb"[ \t]*", data[line_start:offset]).group(0).decode()


def normalise(xml, info):
    """Bring an existing FormMetadata step to the canonical state: rewrite its canonical
    attributes, and move it to first position if needed."""
    data = xml.encode("utf-8")
    node = info["node"]
    stats = {"normalised": 0, "moved": 0, "attrs": {}, "kept_extra_attrs": extra_attrs(node),
             "extra_children": extra_children(node)}
    start, end = node_span(data, node)
    new_tag, changed = normalise_tag(data[start:tag_span(data, node.start)[1]], node)
    stats["attrs"] = {k: {"before": b, "after": a} for k, (b, a) in changed.items()}
    subtree = new_tag + data[tag_span(data, node.start)[1]:end]

    if info["index"] == 0:
        if not changed:
            return xml, stats
        stats["normalised"] = 1
        return (data[:start] + subtree + data[end:]).decode("utf-8"), stats

    # not first → cut the subtree out (with its leading whitespace) and splice it back in right
    # after the root `items` open tag, at the indentation of the current first step.
    stats["normalised"] = 1 if changed else 0
    stats["moved"] = 1
    cut = start
    while cut > 0 and data[cut - 1:cut] in (b" ", b"\t"):
        cut -= 1
    if cut > 0 and data[cut - 1:cut] == b"\n":
        cut -= 1
    _, items_end = tag_span(data, info["items"].start)
    indent = indent_of(data, info["steps"][0].start)
    data = data[:cut] + data[end:]                                   # remove (later offset first)
    block = ("\n" + indent).encode("utf-8") + subtree
    data = data[:items_end] + block + data[items_end:]
    return data.decode("utf-8"), stats


def transform(xml, entry):
    """(new_xml, stats) — pure, does not touch the ZIP."""
    stats = {"inserted": 0, "already_present": False, "skipped": None, "tag": None,
             "normalised": 0, "moved": 0}
    info = inspect(xml)
    if info["items"] is None:
        stats["skipped"] = "no_items"
        return xml, stats
    if info["present"]:
        stats["already_present"] = True
        if not deviations(info["node"]) and info["index"] == 0:
            stats["skipped"] = "already_canonical"
            return xml, stats
        new_xml, nstats = normalise(xml, info)
        stats.update(nstats)
        return new_xml, stats
    if info["nested"]:
        stats["skipped"] = "misplaced"          # present but nested — a human moves it
        return xml, stats

    data = xml.encode("utf-8")
    items = info["items"]
    _, items_end = tag_span(data, items.start)
    if data[items_end - 2:items_end] == b"/>":
        stats["skipped"] = "items_self_closed"      # no steps to insert before
        return xml, stats

    steps = info["steps"]
    indent = indent_of(data, steps[0].start) if steps else indent_of(data, items.start) + "    "
    tag = deterministic_tag(entry)
    stats["tag"] = tag
    block = ("\n" + indent + node_xml(tag, indent)).encode("utf-8")
    data = data[:items_end] + block + data[items_end:]
    stats["inserted"] = 1
    return data.decode("utf-8"), stats


def selfcheck(xml):
    """(parses, canonical) — is the fragment the first step *and* canonically shaped?"""
    try:
        info = inspect(xml)
    except Exception:
        return False, False
    if not info["present"] or info["index"] != 0:
        return True, False
    return True, not deviations(info["node"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--stage", default="_edit")
    args = ap.parse_args()

    inner, xml = form_entry_and_xml(args.zip)
    if not inner:
        print("error: no form .content.xml (cq:Page) found in ZIP", file=sys.stderr)
        sys.exit(1)

    new_xml, stats = transform(xml, inner)
    parses, canonical = selfcheck(new_xml)
    stats.update({"inner": inner, "well_formed": parses, "canonical_first": canonical,
                  "changed": new_xml != xml,
                  "ok": bool(parses and canonical)})

    if new_xml != xml and not args.dry_run:
        dest = os.path.join(args.stage, inner)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(new_xml)
        out = subprocess.run(["zip", "-q", os.path.abspath(args.zip), inner],
                             cwd=args.stage, capture_output=True, text=True)
        if out.returncode != 0:
            print(out.stderr, file=sys.stderr)
            sys.exit(1)

    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
