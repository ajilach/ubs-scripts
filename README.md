# ubs-scripts

Scripts for checking and fixing UBS AEM Adaptive Form packages.

| Script | What it does |
|---|---|
| `check_form_features.py` | Reports which agreed features each package has. Changes nothing. |
| `fix_form_features.py` | Enables the missing features and writes fixed copies of the packages. |

Typical workflow: export the packages from AEM, run the check, run the fix into a new folder,
run the check again on that folder, then install the fixed packages in AEM.

```bash
python3 check_form_features.py exported/
python3 fix_form_features.py exported/ --out fixed/
python3 check_form_features.py fixed/
```

## check_form_features.py

Reads exported AEM form packages (ZIP files) and checks that each form has the agreed
features enabled. The script only reads the packages and never changes them.

### Requirements

Python 3.8 or newer. Standard library only, nothing to install.

### What it checks

| Column | Passes when |
|---|---|
| Redacto DoR | The form metadata has `redactoSummary="true"` (UBS properties: "Activates Redacto Summary for this form"). |
| HTML preview/summary | The form has a `summaryPanel` step containing the summary component `summaryComponent`. |
| Save as draft | The form metadata `menuOptions` contains `ajila-forms-ubs-menu-option-save` (UBS properties: "Save as draft"). |
| Print incomplete | The form metadata has `printPartialMode="true"` (UBS properties: "Print partially" = "Active"). Required on Germany forms only (`afforms_germany_all`); other forms show `n/a`. |
| Show only input fields | The FormMetadata fragment (`afforms_global_fragmentlib/formmetadata`) is a step of the form. The Forms Workbench button "Show only input fields" writes to its field `txtShowOnlyInputFields`, which does not exist without the fragment. |

### Usage

Export the packages from AEM Package Manager (Tools > Deployment > Packages, select the
package, Download), then run:

```bash
python3 check_form_features.py path/to/packages
```

The path can be a single ZIP or a folder; subfolders are searched too.

Options:

| Option | Effect |
|---|---|
| `--csv report.csv` | Also write the results to a CSV file. |
| `--json` | Print JSON instead of the table. |

### Output

One row per package with `ok`, `MISSING`, `n/a` or `ERROR` (package could not be read) per
check, followed by the details of what is missing for each failing package and a summary line:

```
Package       Redacto DoR  HTML preview/summary  Save as draft  Print incomplete  Show only input fields
------------  -----------  --------------------  -------------  ----------------  ----------------------
AAAE_019.zip  ok           ok                    ok             MISSING           MISSING
AAJB_033.zip  ok           ok                    ok             n/a               ok

Details:
  AAAE_019.zip
    Print incomplete:      printPartialMode is not set
    Show only input fields: no FormMetadata fragment step (txtShowOnlyInputFields missing)

1 of 2 packages pass all checks.
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every package passes every check. |
| 1 | At least one check fails, or a package cannot be read. |
| 2 | Wrong path, or no ZIP files found. |

### Limits

- The script checks the package files, not what is currently installed on an AEM instance.
  Re-export the package after changing a form in AEM.
- It does not check how the form looks or what the generated PDF contains; that still needs
  a visual review.

## fix_form_features.py

Enables every feature that `check_form_features.py` checks, so that after a successful run
every check passes. Fixed packages are written as copies to a separate folder; the input ZIPs
are never modified. Only the affected attributes and nodes change, the rest of each package
stays byte for byte the same.

### Requirements

Python 3.8 or newer, standard library only. Keep the repository layout as it is: the script
uses `check_form_features.py` next to it to re-check every fixed package, and the structural
fixes in `vendor/`.

### What it changes

| Feature | Change |
|---|---|
| HTML preview/summary | A missing summary step is built: a `summaryPanel` step before the preview step, holding the summary component, with the form's own `doroptionsubs` and metadata controls moved into it. The toolbar's old preview button is removed and the Next button fills the summary. |
| Redacto DoR | Sets `redactoSummary="true"` in the form metadata. |
| Save as draft | Adds `ajila-forms-ubs-menu-option-save` to the form metadata `menuOptions`. |
| Print incomplete | Sets `printPartialMode="true"` in the form metadata, on Germany forms only. |
| Show only input fields | Inserts the FormMetadata fragment as the first, hidden wizard step, or brings an existing one to that position and shape. |

The summary step and the FormMetadata step are built by the same code the
ajila-forms-conversion-feedback sweeps use (`summary-step-redacto`, `formmetadata-step`),
copied unchanged into `vendor/`. See [vendor/README.md](vendor/README.md).

### Usage

```bash
python3 fix_form_features.py path/to/packages --out path/to/fixed
python3 fix_form_features.py path/to/packages --dry-run
```

| Option | Effect |
|---|---|
| `--out FOLDER` | Write the fixed packages to this folder. Must not be the input folder. |
| `--dry-run` | Only report what would change, write nothing. |
| `--json` | Print JSON instead of text. |

Exactly one of `--out` and `--dry-run` is required. Only packages that changed are written.

### Output

One block per package with its status and what changed:

```
BlueprintFormsPackage_AAGS.zip  [FIXED]
    summary        summary step: panel, preview-button, hook, redacto, rename:metadataTextDraw
    input_fields   inserted FormMetadata step
    metadata       menuOptions: not set -> "[ajila-forms-ubs-menu-option-save]"
    metadata       printPartialMode: not set -> "true"
    written: fixed/BlueprintFormsPackage_AAGS.zip
```

Status values: `FIXED` (changed and written), `OK` (nothing to do), `ERROR` (the package could
not be read, could not be fixed, or the fixed copy failed its re-check; nothing is written for
it, and the message says why).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every package passes every check after fixing. |
| 1 | At least one package could not be fixed. |
| 2 | Wrong path, no ZIP files found, or `--out`/`--dry-run` missing. |

### After fixing

Install the fixed packages through AEM Package Manager and look at a few forms:

- the form opens on its first real step,
- the summary step lists the entered values before submitting,
- on Germany forms the "Incomplete form mode" toggle appears in the header, and Download in
  that mode produces the PDF.
