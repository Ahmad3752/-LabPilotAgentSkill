# Independent review rubric (skill step 11)

Review the finished lab the way a strict lab engineer would. Use only:
- the original manual (`inputs/` in the run folder), and any earlier manual it depends on
- the rendered Word pages (`page-*.png` in `pages_dir` from the `finish` JSON)
- the executed notebook (`notebook/executed.ipynb`)

Do not trust the author's summaries. For each item, report a problem only when you find concrete evidence: quote it and give its location.

## Coverage
- Every task, part and question in the manual has an answer, in the right box and in the manual's order.
- Sections the introduction promises (teardown, cleanup) are present.
- Reads like the student's own report: no LaTeX, rounded numbers, no tool wording (LabPilot, "the student", Colab runtime).
- Every TODO in the starter code is completed. Every table the manual asks to fill is filled.
- Things the manual asks to show (schemas, counts, plots, job counters, `ls` listings) are visible in the screenshots.

## Correctness
- Each numeric claim in an answer matches the notebook output exactly.
- Outputs contain no hidden failures: "No such file", tracebacks, "JOB FAILED", empty results, checks that tested nothing.
- Explanations are technically right, and the reasoning matches what was observed, not just the textbook expectation.
- Where results differ from what the manual implies, the answer says so and explains why.

## Quality
- Answers are clear sentences a student could say aloud and defend. They contain no variable dumps such as `X = True` and no log lines.
- Answers are concise and specific to this run's data. Each answer addresses every sub-part of its question.
- Screenshots are readable at page width, not clipped, and in order. Captions are short.
- The original manual content is intact: logo, headers and footers, instructions, starter code.

## Output format
Return a list of findings, most severe first. For each finding give:
- severity: `would-lose-marks`, `minor` or `cosmetic`
- location: question, page or cell
- evidence
- the concrete fix

Say explicitly if nothing would lose marks.
