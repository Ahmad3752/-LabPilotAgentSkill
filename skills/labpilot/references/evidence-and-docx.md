# Evidence and the Word copy

## Screenshots
- `labpilot capture` renders the executed notebook to HTML with scripts stripped. It opens the page from a local file in headless Chromium, with JavaScript disabled and all network requests blocked, then screenshots each passed cell.
- It produces one PNG with code and output for each cell. Tall cells are split into a code image plus output parts, cut at output boundaries. Wide outputs widen the viewport instead of being clipped.
- Every image has an evidence record: run, attempt, task, cell, code hash, backend, capture mode, timestamp, sha256 and size. Images from an old attempt or for changed code are marked stale, and export will not use them.
- Each image is labelled "local rendering of the notebook executed on <backend>". These are not Google Colab UI screenshots, and you must never describe them as such.
- Problems you need to fix:
  - A cell produced no output when output was expected.
  - An image is blank or does not decode.

## Word output
- LabPilot always starts from the pristine original manual, so re-exports never duplicate content. The original file is never modified.
- Inside each solution anchor it removes only placeholder text (for example "Paste your code here"). It then adds, for each part in order: a subheading, a short explanation, the screenshots (inline, sized to the cell width) and the Q&A.
- Exact-height and can't-split table rows are relaxed so content can flow across pages. Every such change is listed in REVIEW.md.
- If an anchor cannot be resolved reliably, the solution goes into a labelled appendix and the item is flagged for review. It is never pasted into an unrelated section.
- The file is named `<Lab>_Completed.docx` only when every check passes:
  - inputs unchanged
  - datasets present
  - all tasks executed
  - valid evidence
  - verified answers
  - document structure
  - native-Colab evidence, if the manual requires it

  Otherwise it is named `<Lab>_Partial.docx`, and REVIEW.md lists what remains.
- Structural validation checks:
  - the original text is preserved in order
  - every screenshot is embedded exactly once, byte for byte
  - original images, headers and footers, and page setup are unchanged
- Optional page rendering uses LibreOffice when it is installed. If it is not, that check is reported as unavailable.
