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

Enables the features that `check_form_features.py` reports as missing. Fixed packages are
written as copies to a separate folder; the input ZIPs are never modified. Only the affected
attributes and the inserted step change, the rest of each package stays byte for byte the same.

### Requirements

Python 3.8 or newer, standard library only. `check_form_features.py` must be in the same
folder, because the fix script uses it to re-check every fixed package.

### What it changes

| Feature | Change |
|---|---|
| Save as draft | Adds `ajila-forms-ubs-menu-option-save` to the form metadata `menuOptions`. |
| Print incomplete | Sets `printPartialMode="true"` in the form metadata, on Germany forms only. |
| Show only input fields | Inserts the FormMetadata fragment as the first, hidden wizard step (same shape as the `formmetadata-step` sweep in ajila-forms-conversion-feedback). |
| Redacto DoR | Sets `redactoSummary="true"` in the form metadata, but only when the form already has its summary step. |

### What it does not change

These are reported under `REVIEW` and make the exit code 1:

- **Missing summary step.** Redacto builds the DoR from the summary, so enabling Redacto
  without the summary step would produce an empty DoR. The script leaves both off. Add the
  summary step with `set_summary_step.py` in ajila-forms-conversion-feedback, then run the
  fix again.
- **FormMetadata fragment nested inside a panel** instead of being a step. Move it in AEM.

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
Germany_BAGT.zip  [FIXED]
    metadata       printPartialMode: "false" -> "true"
    input_fields   inserted FormMetadata step
    written: fixed/Germany_BAGT.zip
```

Status values: `FIXED` (changed and written), `OK` (nothing to do), `REVIEW` (something left
for a person, see above), `ERROR` (package could not be read or the fixed copy failed its
re-check; nothing is written).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every package ends with every check passing. |
| 1 | At least one package needs review or could not be processed. |
| 2 | Wrong path, no ZIP files found, or `--out`/`--dry-run` missing. |

### After fixing

Install the fixed packages through AEM Package Manager and look at a few forms: the form
opens on its first real step, and on Germany forms the "Incomplete form mode" toggle appears
in the header.
