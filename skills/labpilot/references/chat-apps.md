# Chat app mode (Claude.ai, ChatGPT)

Use this when you run inside a chat app's code sandbox (Linux; the user's uploads are under
`/mnt/user-data/uploads` on Claude.ai or `/mnt/data` on ChatGPT). That sandbox cannot reach the student's Google
Colab, so the student runs the notebook in their own browser, where they are already signed in to Google. They
click once (Run all), download the result and give it back to you. Nothing is installed on their computer.

## Rules that change in this mode
- No LMS. The chat has no Nuts LMS MCP. Ask the student to download the lab files from the LMS themselves and
  upload them here. **Never ask for the LMS (or any) username or password**, even if the student offers it.
- No `labpilot colab-login`, no `doctor` Colab checks, no Codex sandbox notes: they do not apply here.
- The student's name and CMS ID are not saved between chats: pass them to `init` every time.
- Never edit, create or "fix" the notebook the student gives back. Its outputs are the evidence.

## Steps
1. **Install LabPilot in the sandbox** (once per chat):
   `pip install "labpilot @ git+https://github.com/Ahmad3752/-LabPilotAgentSkill"`.
   If the sandbox cannot reach GitHub, tell the student: "This chat can't download LabPilot. Turn on network
   access for code (Claude.ai: Settings > Capabilities > allow network access, or add github.com and pypi.org),
   then start a new chat." Stop there. Do not rewrite LabPilot yourself.
2. **Work folder:** copy the uploaded `.docx` and data files to `/tmp/lab` and work there (`--lab-dir /tmp/lab`,
   or `cd /tmp/lab`). The upload folder can be read-only.
3. **Ask once** as in SKILL.md, with these changes: run only `labpilot colab-check "<manual>" --json` (no doctor),
   and drop the sign-in line. The question: "Reply **yes** with your name and CMS ID. I'll write the notebook,
   you'll run it in your own Google Colab with one click, and I'll fill the Word copy." End your turn.
4. After the yes: `labpilot init --lab "<manual>.docx" --student-name "<name>" --student-id "<id>" --json`, then the
   plan and `cells.json` exactly as in the SKILL.md workflow (steps 1–6).
5. `labpilot finish <id> --cells cells.json --backend colab-web --allow-cloud --allow-upload --json`
   (add `--gpu T4` when colab-check said the GPU is `needed`). `next_action.action` is `run-in-colab`.
6. **Hand the notebook to the student.** Copy `.labpilot/runs/<id>/for_colab.ipynb` to the place the app offers
   downloads from (Claude.ai: `/mnt/user-data/outputs/`; ChatGPT: `/mnt/data/`) as `<Lab name> - run in Colab.ipynb`,
   give them the file, and these steps word for word:
   1. Open **colab.research.google.com** (sign in to Google if it asks).
   2. **File > Upload notebook**, and choose the file I just gave you.
   3. **Runtime > Run all.** When the first cell asks, choose your lab files (<list them>).
   4. When it has finished, **File > Download > Download .ipynb**, and upload that file here.

   End your turn. Do not wait in a loop.
7. When the student uploads the run: `labpilot finish <id> --executed "<uploaded .ipynb>" --json`.
   - `repair`: fix only the failing cells in `cells.json`, run `labpilot finish <id> --cells cells.json --backend
     colab-web --json`, and hand out the new notebook as in step 6 ("one cell needed a fix, please run this new
     one"). A lab that failed like a GPU lab on CPU (CUDA, out of memory): add `--gpu T4`.
   - `run-in-colab` again: some cells did not run or were edited. Hand out the new notebook and say why.
   - `RECORD_INVALID`: the file is not the notebook you gave them. Ask for the downloaded Colab file again.
8. Then continue with SKILL.md workflow steps 8–11 (answers, `finish --answers`, review, final message).
   If `capture` fails with `BROWSER_UNAVAILABLE`, run `python -m playwright install chromium` once and run
   `finish` again. If that fails too, it is a blocker: tell the student, do not fake screenshots.
9. **Deliver:** copy the Completed `.docx`, the executed notebook and `REVIEW.md` from `output_dir` to the
   downloads place from step 6 and give the student the files. Do not run `labpilot clean` (the sandbox is
   thrown away anyway).

Evidence in this mode is labelled "Google Colab (browser, student-run)": LabPilot checks that every cell is its
own unchanged cell with real Colab outputs, but cannot prove the file came from Colab. Say so in the final message.
