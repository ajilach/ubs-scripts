#!/usr/bin/env python3
"""
Enable the agreed features on UBS AEM Adaptive Form packages: the fixing counterpart of
check_form_features.py. After a successful run every check passes.

  summary        a missing summary step is built: a `summaryPanel` step before the preview
                 step holding the summary component, with the form's own `doroptionsubs`
                 and metadata controls moved into it (PROBLEM-summary-step-redacto)
  redacto_dor    DAM metadata `redactoSummary="true"`
  save_as_draft  DAM metadata `menuOptions` gets `ajila-forms-ubs-menu-option-save`
  print_partial  DAM metadata `printPartialMode="true"`, on Germany forms only
  input_fields   the FormMetadata fragment as the form's first, hidden wizard step, inserted
                 or brought to its canonical shape (PROBLEM-formmetadata-step)

The two structural fixes are the ones from ajila-forms-conversion-feedback, copied
unchanged into vendor/ (see vendor/README.md). Only the affected attributes and nodes
change; the rest of each file is kept byte for byte. Every fixed package is re-checked with
check_form_features.py before it is written, and a package that cannot be brought to a
passing state is reported as an error and not written. Fixed packages go to --out as copies;
the input ZIPs are never modified. Python 3.8+, standard library only.

Usage:
    python3 fix_form_features.py PACKAGE.zip [...] --out FIXED_DIR
    python3 fix_form_features.py FOLDER --out FIXED_DIR
    python3 fix_form_features.py FOLDER --dry-run          # report only, write nothing

Exit code: 0 if every package passes every check after fixing, 1 if any package could not
be fixed, 2 on bad usage.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from xml.parsers import expat

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "vendor"))
import check_form_features as check  # noqa: E402
import set_formmetadata_step as formmetadata  # noqa: E402
import set_summary_step as summary  # noqa: E402
import summary_step  # noqa: E402

METADATA_TAG = re.compile(rb'<metadata\b(?:\s+[\w:.\-]+="[^"]*")*\s*/?>', re.S)
ATTR_SPAN = re.compile(rb'(\s+)([\w:.\-]+)="([^"]*)"')
# The summary fixer completes in two passes (the second names the moved controls); a third
# pass must change nothing.
SUMMARY_PASSES = 3


class FixError(Exception):
    """The package cannot be brought to a passing state by this script."""


# --------------------------------------------------------------------------- DAM metadata

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
    options = check.multi_value(current)
    if check.SAVE_OPTION in options:
        return None
    return "[" + ",".join(options + [check.SAVE_OPTION]) + "]"


def set_true(current):
    return None if current == "true" else "true"


# --------------------------------------------------------------------------- structure

def fix_summary(form, dam, locales):
    """Build the summary step with the vendored fixer, repeated until it changes nothing.
    Returns (form, dam, [actions])."""
    actions = []
    for _ in range(SUMMARY_PASSES):
        new_form, new_dam, report = summary.apply(form, dam, locales)
        if (new_form, new_dam) == (form, dam):
            break
        actions += [a for a in report["actions"] if a not in actions]
        form, dam = new_form, new_dam
    final = summary_step.state_from_bytes(form, dam, locales)
    if not summary_step.is_complete(final):
        _, _, report = summary.apply(form, dam, locales)
        raise FixError("summary step could not be completed: %s"
                       % report.get("flag", "unknown reason"))
    return form, dam, actions


def fix_formmetadata(form, form_path):
    """Insert or normalise the FormMetadata step with the vendored fixer.
    Returns (form, change description or None)."""
    new_xml, stats = formmetadata.transform(form.decode("utf-8"), form_path)
    if stats["skipped"] not in (None, "already_canonical"):
        raise FixError("FormMetadata step could not be fixed: %s" % stats["skipped"])
    if stats["inserted"]:
        return new_xml.encode("utf-8"), "inserted FormMetadata step"
    if stats["normalised"] or stats["moved"]:
        what = ["moved to first step"] if stats["moved"] else []
        what += sorted(stats["attrs"]) and ["set " + ", ".join(sorted(stats["attrs"]))]
        return new_xml.encode("utf-8"), "FormMetadata step: " + "; ".join(what)
    return form, None


# --------------------------------------------------------------------------- package

def entries(z):
    """(dam_path, form_path) inside the package."""
    dam = next((n for n in z.namelist() if n.startswith(check.DAM_ROOT)
                and n.endswith("/.content.xml") and b"dorType" in z.read(n)), None)
    form = next((n for n in z.namelist() if n.startswith(check.FORM_ROOT)
                 and n.endswith("/.content.xml") and b'"cq:Page"' in z.read(n)), None)
    if dam is None or form is None:
        raise FixError("not an adaptive form package (DAM asset or form page missing)")
    return dam, form


def fix(dam, form, form_path, locales):
    """Pure transform of the two package entries. Returns (dam, form, changes)."""
    changes = {}
    if not check.check_summary(check.parse_prefixed(form))[0]:
        form, dam, actions = fix_summary(form, dam, locales)
        changes["summary"] = ["summary step: " + ", ".join(actions)]

    form, step_change = fix_formmetadata(form, form_path)
    if step_change:
        changes["input_fields"] = [step_change]

    wants = [("redactoSummary", set_true), ("menuOptions", with_save_option)]
    if check.is_germany(form_path):
        wants.append(("printPartialMode", set_true))
    dam, meta_changes = fix_metadata(dam, wants)
    if meta_changes:
        changes["metadata"] = meta_changes
    return dam, form, changes


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
    row = {"package": zip_path, "status": None, "changes": {}, "written": None, "error": None}
    scratch = tempfile.mkdtemp(prefix="form_features_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            dam_path, form_path = entries(z)
            dam, form = z.read(dam_path), z.read(form_path)
        locales = summary_step.dictionary_locales(zip_path)
        new_dam, new_form, changes = fix(dam, form, form_path, locales)
        row["changes"] = changes

        tmp = os.path.join(scratch, os.path.basename(zip_path))
        write_copy(zip_path, tmp, {dam_path: new_dam, form_path: new_form})
        # Hard post-condition: the copy passes every check.
        result = check.check_package(tmp)
        failing = [k for k, c in result["checks"].items() if c["ok"] is False]
        if result["error"] or failing:
            raise FixError("fixed copy still fails: %s" % (result["error"] or ", ".join(failing)))

        if changes and out_dir:
            dst = os.path.join(out_dir, os.path.basename(zip_path))
            shutil.move(tmp, dst)
            row["written"] = dst
        row["status"] = "fixed" if changes else "ok"
    except (zipfile.BadZipFile, OSError, ValueError, expat.ExpatError, FixError) as exc:
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
