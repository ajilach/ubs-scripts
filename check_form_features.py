#!/usr/bin/env python3
"""
Check that UBS AEM Adaptive Form packages have the four agreed features enabled:

  redacto_dor    Redacto DoR        DAM metadata `redactoSummary="true"`
                                    (UBS properties: "Activates Redacto Summary for this form")
  summary        HTML preview /     a `summaryPanel` step holding the `controls/summary`
                 summary            component `summaryComponent`
  save_as_draft  Save as draft      DAM metadata `menuOptions` contains
                                    `ajila-forms-ubs-menu-option-save`
                                    (UBS properties: "Save as draft")
  print_partial  Print incomplete   DAM metadata `printPartialMode="true"`
                                    (UBS properties: "Print partially" = "Active").
                                    Germany forms (`afforms_germany_all`) only; other
                                    forms report "n/a"
  input_fields   Show only input    the FormMetadata fragment is a wizard step. The
                 fields             Forms Workbench button "Show only input fields" writes
                                    to its field `txtShowOnlyInputFields`; without the
                                    fragment that field does not exist

All four are read from the package ZIP as exported from AEM (Package Manager), so the
result reflects what the package installs. Nothing is changed. Python 3.8+, standard
library only.

Usage:
    python3 check_form_features.py PACKAGE.zip [PACKAGE.zip ...]
    python3 check_form_features.py FOLDER            # every *.zip below FOLDER
    python3 check_form_features.py FOLDER --json     # machine-readable output
    python3 check_form_features.py FOLDER --csv report.csv

Exit code: 0 if every package passes every check, 1 if any check fails or a package
cannot be read, 2 on bad usage.
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

DAM_ROOT = "jcr_root/content/dam/formsanddocuments/"
FORM_ROOT = "jcr_root/content/forms/af/"

SUMMARY_RT = "ajila-forms-customers/ajila-forms-ubs/components/controls/summary"
SAVE_OPTION = "ajila-forms-ubs-menu-option-save"
FORMMETADATA_FRAGMENT = "/content/dam/formsanddocuments/afforms_global_fragmentlib/formmetadata"
GERMANY_FOLDER = "afforms_germany_all"

CHECKS = (
    ("redacto_dor", "Redacto DoR"),
    ("summary", "HTML preview/summary"),
    ("save_as_draft", "Save as draft"),
    ("print_partial", "Print incomplete"),
    ("input_fields", "Show only input fields"),
)


class PackageError(Exception):
    """The package does not have the shape of an exported Adaptive Form."""


# --------------------------------------------------------------------------- XML

def parse_prefixed(data):
    """Parse JCR FileVault XML, giving tags and attribute names back in their
    prefixed form (`sling:resourceType`, `fd:click`) instead of `{uri}local`."""
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


def child(el, tag):
    return next((c for c in el if c.tag == tag), None) if el is not None else None


def multi_value(raw):
    """A JCR multi-value property `[a,b]` as a list (single values as a one-item list)."""
    if raw is None:
        return []
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [v.strip() for v in raw.split(",") if v.strip()]


# --------------------------------------------------------------------------- package

def read_package(zip_path):
    """(dam_metadata_attrs, form_root, form_entry_path) for one package."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        dam = None
        for n in names:
            if n.startswith(DAM_ROOT) and n.endswith("/.content.xml"):
                data = z.read(n)
                if b"dorType" in data:
                    dam = data
                    break
        form, form_path = None, None
        for n in names:
            if n.startswith(FORM_ROOT) and n.endswith("/.content.xml"):
                data = z.read(n)
                if b'"cq:Page"' in data:
                    form, form_path = data, n
                    break
    if dam is None:
        raise PackageError("no form asset metadata (DAM .content.xml with dorType)")
    if form is None:
        raise PackageError("no adaptive form page (.content.xml of type cq:Page)")

    metadata = find_first(parse_prefixed(dam), lambda el: el.tag == "metadata")
    if metadata is None:
        raise PackageError("DAM asset has no <metadata> node")
    return metadata.attrib, parse_prefixed(form), form_path


def form_name(zip_path, form_root):
    page = find_first(form_root, lambda el: el.tag == "jcr:content")
    title = page.attrib.get("jcr:title") if page is not None else None
    return title or os.path.splitext(os.path.basename(zip_path))[0]


# --------------------------------------------------------------------------- checks

def check_redacto(meta):
    value = meta.get("redactoSummary")
    if value == "true":
        return True, 'redactoSummary="true"'
    return False, "redactoSummary is %s" % ('"%s"' % value if value is not None else "not set")


def wizard_steps(form_root):
    """The children of the root panel's `items` (the wizard steps), or None."""
    root_panel = find_first(form_root, lambda el: el.attrib.get("name") == "guideRootPanel"
                            or el.tag == "rootPanel")
    return child(root_panel, "items")


def check_summary(form_root):
    steps = wizard_steps(form_root)
    if steps is None:
        return False, "form has no root panel"

    # No check on the Next button: the UBS app's `navigation.nextStep` fills the summary
    # itself whenever the page holds a summary component (since 2026-05-19), so a form
    # without its own `setSummaryData` call still shows a filled summary.
    panel = next((s for s in steps if s.attrib.get("name") == "summaryPanel"), None)
    if panel is None:
        return False, "no summaryPanel step"
    if not any(el.attrib.get("sling:resourceType") == SUMMARY_RT
               and el.attrib.get("name") == "summaryComponent" for el in panel.iter()):
        return False, "summaryPanel has no summaryComponent"
    return True, "summary step with summary component present"


def check_save_as_draft(meta):
    options = multi_value(meta.get("menuOptions"))
    if SAVE_OPTION in options:
        return True, "menuOptions contains %s" % SAVE_OPTION
    return False, "menuOptions is %s" % (meta.get("menuOptions") or "not set")


def is_germany(form_path):
    return GERMANY_FOLDER in form_path.split("/")


def check_print_partial(meta, form_path):
    """Required on Germany forms only; None (not applicable) elsewhere."""
    if not is_germany(form_path):
        return None, "not a Germany form"
    value = meta.get("printPartialMode")
    if value == "true":
        return True, 'printPartialMode="true"'
    return False, "printPartialMode is %s" % ('"%s"' % value if value is not None else "not set")


def check_input_fields(form_root):
    steps = wizard_steps(form_root)
    if steps is None:
        return False, "form has no root panel"
    for step in steps:
        if step.attrib.get("fragRef", "").rstrip("/") == FORMMETADATA_FRAGMENT:
            return True, "FormMetadata fragment step present"
    return False, "no FormMetadata fragment step (txtShowOnlyInputFields missing)"


def check_package(zip_path):
    """One result row: {package, form, ok, error, checks: {key: {ok, detail}}}."""
    row = {"package": zip_path, "form": None, "ok": False, "error": None, "checks": {}}
    try:
        meta, form_root, form_path = read_package(zip_path)
    except (zipfile.BadZipFile, OSError, ET.ParseError, PackageError) as exc:
        row["error"] = str(exc)
        return row
    row["form"] = form_name(zip_path, form_root)
    results = {
        "redacto_dor": check_redacto(meta),
        "summary": check_summary(form_root),
        "save_as_draft": check_save_as_draft(meta),
        "print_partial": check_print_partial(meta, form_path),
        "input_fields": check_input_fields(form_root),
    }
    # ok is True, False, or None for a check that does not apply to this form.
    row["checks"] = {k: {"ok": ok, "detail": detail} for k, (ok, detail) in results.items()}
    row["ok"] = all(ok is not False for ok, _ in results.values())
    return row


# --------------------------------------------------------------------------- CLI

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


STATUS_TEXT = {True: "ok", False: "MISSING", None: "n/a"}


def print_table(rows):
    headers = ["Package"] + [label for _, label in CHECKS]
    lines = []
    for r in rows:
        name = os.path.basename(r["package"])
        if r["error"]:
            lines.append([name] + ["ERROR"] * len(CHECKS))
        else:
            lines.append([name] + [STATUS_TEXT[r["checks"][k]["ok"]] for k, _ in CHECKS])
    widths = [max(len(h), *(len(l[i]) for l in lines)) if lines else len(h)
              for i, h in enumerate(headers)]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    print(fmt % tuple(headers))
    print(fmt % tuple("-" * w for w in widths))
    for l in lines:
        print(fmt % tuple(l))

    problems = [r for r in rows if not r["ok"]]
    if problems:
        print("\nDetails:")
        for r in problems:
            print("  %s" % os.path.basename(r["package"]))
            if r["error"]:
                print("    cannot read package: %s" % r["error"])
                continue
            for k, label in CHECKS:
                if r["checks"][k]["ok"] is False:
                    print("    %-22s %s" % (label + ":", r["checks"][k]["detail"]))
    passed = sum(1 for r in rows if r["ok"])
    print("\n%d of %d packages pass all checks." % (passed, len(rows)))


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["package", "form", "all_ok", "error"]
                   + [k for k, _ in CHECKS] + [k + "_detail" for k, _ in CHECKS])
        for r in rows:
            checks = r["checks"]
            w.writerow([os.path.basename(r["package"]), r["form"] or "", r["ok"], r["error"] or ""]
                       + [STATUS_TEXT[checks[k]["ok"]] if checks else "" for k, _ in CHECKS]
                       + [checks[k]["detail"] if checks else "" for k, _ in CHECKS])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("paths", nargs="+", help="package ZIP(s) or folder(s) containing them")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--csv", metavar="FILE", help="also write the results to a CSV file")
    args = ap.parse_args(argv)

    zips = collect(args.paths)
    if not zips:
        print("no .zip packages found", file=sys.stderr)
        return 2
    rows = [check_package(z) for z in zips]

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print_table(rows)
    if args.csv:
        write_csv(rows, args.csv)
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
