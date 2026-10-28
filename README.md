# ubs-scripts

Scripts for UBS AEM Adaptive Form packages.

## fix_form_features.py

Checks exported AEM form packages (ZIP files) for the agreed features, fixes the missing
ones, writes fixed copies, and prints a report of what was already there and what was fixed.
The input ZIPs are never modified.

### Requirements

Python 3.8 or newer. One file, standard library only, nothing to install.

### Features

| Feature | Target state | Fix |
|---|---|---|
| Redacto DoR | Form metadata `redactoSummary="true"` (UBS properties: "Activates Redacto Summary for this form"). | Sets the property. |
| HTML preview/summary | A `summaryPanel` step containing the summary component. | Builds the step before the preview step, moves the form's own `doroptionsubs` and metadata controls into it, removes the old preview button and makes the Next button fill the summary. |
| Save as draft | Form metadata `menuOptions` contains `ajila-forms-ubs-menu-option-save` (UBS properties: "Save as draft"). | Adds the option. |
| Print incomplete | Form metadata `printPartialMode="true"` (UBS properties: "Print partially" = "Active"). Germany forms only (`afforms_germany_all`); other forms show `n/a`. | Sets the property. |
| Show only input fields | The FormMetadata fragment (`afforms_global_fragmentlib/formmetadata`) is the first, hidden step. The Forms Workbench button "Show only input fields" writes to its field `txtShowOnlyInputFields`. | Inserts the step, or moves an existing one to the front and gives it the canonical attributes. |

The summary step and the FormMetadata step are built the same way as the sweeps
`summary-step-redacto` and `formmetadata-step` in ajila-forms-conversion-feedback. Only the
affected attributes and nodes change; everything else in a package stays byte for byte the
same.

### Usage

Export the packages from AEM Package Manager (Tools > Deployment > Packages, select the
package, Download), then run:

```bash
python3 fix_form_features.py path/to/packages --out path/to/fixed
```

The path can be a single ZIP or a folder; subfolders are searched too. Only packages that
needed a change are written to the output folder.

| Option | Effect |
|---|---|
| `--out FOLDER` | Write the fixed packages to this folder. Must not be the input folder. |
| `--dry-run` | Only report, write nothing. Use this to check packages without fixing them. |
| `--csv report.csv` | Also write the report to a CSV file. |
| `-v`, `--verbose` | List every change made. |
| `--json` | Print JSON instead of the table. |

Exactly one of `--out` and `--dry-run` is required.

### Report

One row per package and feature:

```
Package                         Redacto DoR  HTML preview/summary  Save as draft  Print incomplete  Show only input fields  Result
------------------------------  -----------  --------------------  -------------  ----------------  ----------------------  ------
AAAE_019.zip                    ok           ok                    ok             fixed             fixed                   FIXED
AAJB_033.zip                    ok           ok                    ok             n/a               ok                      OK
BlueprintFormsPackage_AAGS.zip  fixed        fixed                 fixed          fixed             fixed                   FIXED
broken.zip                      -            -                     -              -                 -                       ERROR

Errors:
  broken.zip: File is not a zip file

4 packages: 1 error, 2 fixed, 1 ok
```

| Value | Meaning |
|---|---|
| `ok` | The feature was already there. |
| `fixed` | The feature was missing and has been fixed. |
| `n/a` | The feature does not apply to this form. |
| `FAILED` | The feature could not be fixed. |

Result `FIXED` means the package was changed and written, `OK` means nothing needed to change,
and `ERROR` means the package could not be read or fixed; nothing is written for it and the
reason is listed under "Errors". Every fixed package is read back and checked again before it
counts as fixed.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every package has every feature after fixing. |
| 1 | At least one package could not be fixed. |
| 2 | Wrong path, no ZIP files found, or `--out`/`--dry-run` missing. |

### After fixing

Install the fixed packages through AEM Package Manager and look at a few forms:

- the form opens on its first real step,
- the summary step lists the entered values before submitting,
- on Germany forms the "Incomplete form mode" toggle appears in the header, and Download in
  that mode produces the PDF.

The script checks the package files only. It does not check how a form looks or what the
generated PDF contains.
