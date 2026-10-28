# ubs-scripts

Scripts for checking UBS AEM Adaptive Form packages.

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
