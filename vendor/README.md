# vendor

Modules copied unchanged from
[ajila-forms-conversion-feedback](https://github.com/ajilach/ajila-forms-conversion-feedback)
`.claude/scripts/` at commit `a2421999`. `fix_form_features.py` uses them for the two
structural fixes, so the result is the same as the sweeps in that repository.

| File | Used for |
|---|---|
| `set_summary_step.py`, `summary_step.py` | Building the summary step (sweep `summary-step-redacto`). |
| `set_formmetadata_step.py`, `formmetadata_step.py` | Inserting or normalising the FormMetadata step (sweep `formmetadata-step`). |
| `ui_translations.py`, `data/translations/` | The summary step heading in the form's language. |
| `repeatable_add_label.py`, `data/concepts.json`, `data/fragment_titles.json` | Detecting the form's language. |

Do not edit these files here. To update them, copy the current versions from
ajila-forms-conversion-feedback and run `fix_form_features.py` against a set of packages
again; the script re-checks every package it writes.
