"""LabPilot command-line interface (the deterministic tool layer the host agent drives)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from labpilot import __version__
from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.security import redact_obj

app = typer.Typer(add_completion=False, no_args_is_help=True, help="LabPilot: run lab manuals, capture real evidence, "
                  "fill a copy of the original Word file. AI reasoning comes from your coding agent (Codex / Claude Code).")

JSON_OPT = typer.Option(False, "--json", help="Machine-readable JSON on stdout (stable schema).")
LAB_DIR_OPT = typer.Option(None, "--lab-dir", help="Lab folder containing .labpilot/ (default: current folder).")


def _emit(result: dict[str, Any], as_json: bool, human: Callable[[dict[str, Any]], str] | None = None) -> None:
    if as_json:
        sys.stdout.write(json.dumps(redact_obj(result, home=False), indent=2, ensure_ascii=False, default=str) + "\n")
    else:
        sys.stdout.write((human(result) if human else _pretty(result)) + "\n")


def _pretty(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_pretty(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {v}")
        return "\n".join(lines)
    if isinstance(obj, list):
        return "\n".join(f"{pad}- {json.dumps(x, default=str, ensure_ascii=False)[:300]}" if isinstance(x, (dict, list))
                         else f"{pad}- {x}" for x in obj)
    return f"{pad}{obj}"


def _run(fn: Callable[[], dict[str, Any]], as_json: bool, human: Callable[[dict[str, Any]], str] | None = None,
         incomplete: Callable[[dict[str, Any]], bool] | None = None) -> None:
    try:
        result = fn()
    except LabPilotError as exc:
        err = {"ok": False, "error": exc.to_dict()}
        if as_json:
            _emit(err, True)
        else:
            sys.stderr.write(f"error [{exc.code}]: {exc.message}\n")
            for p in exc.details.get("problems", []) if isinstance(exc.details, dict) else []:
                sys.stderr.write(f"  - {p}\n")
            if exc.details.get("findings"):
                for f in exc.details["findings"]:
                    sys.stderr.write(f"  - [{f['rule']}] {f['cell_id']}: {f['message']} :: {f['excerpt']}\n")
            if exc.next_action:
                sys.stderr.write(f"next: {exc.next_action}\n")
        raise typer.Exit(int(exc.exit_code)) from None
    except KeyboardInterrupt:
        sys.stderr.write("interrupted; run state was saved\n")
        raise typer.Exit(int(ExitCode.CANCELLED)) from None
    except Exception as exc:  # noqa: BLE001 - last-resort structured error; details go to stderr
        import traceback

        from labpilot.security import redact

        sys.stderr.write(redact(traceback.format_exc()))
        err = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": redact(f"{type(exc).__name__}: {exc}"),
                                      "retryable": False, "next_action": "Report this bug with the stderr output."}}
        if as_json:
            _emit(err, True)
        raise typer.Exit(1) from None
    _emit({"ok": True, **result}, as_json, human)
    if incomplete and incomplete(result):
        raise typer.Exit(int(ExitCode.INCOMPLETE))


def _open(run_id: str, lab_dir: Path | None):  # noqa: ANN202
    from labpilot.workspace import open_run

    return open_run(run_id, lab_dir)


def _locked(run_id: str, lab_dir: Path | None, fn: Callable[[Any], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    def inner() -> dict[str, Any]:
        from labpilot.cleanup import ensure_not_cleaned

        run = _open(run_id, lab_dir)
        ensure_not_cleaned(run)  # a cleaned run has no working files left to modify
        with run.lock():
            return fn(run)

    return inner


# ---------------------------------------------------------------- setup / doctor


@app.command()
def version() -> None:
    """Print the LabPilot version."""
    print(__version__)


@app.command()
def doctor(json_out: bool = JSON_OPT, project_dir: Path = typer.Option(Path.cwd, "--project-dir")) -> None:  # noqa: B008
    """Inspect installation and capabilities (read-only; allocates no cloud compute)."""
    from labpilot.doctor import doctor as run_doctor

    def human(r: dict[str, Any]) -> str:
        def mark(ok: Any) -> str:
            return "OK " if ok else "-- "

        lines = [f"LabPilot {r['labpilot']} on {r['os']['system']} {r['os']['release']} (Python {r['python']['version']})"]
        for a, info in r["agents"].items():
            lines.append(f"{mark(info['available'])}agent {a}: {info.get('version') or info.get('fix', '')}")
        for k, s in r["skills"].items():
            if s.get("installed"):
                lines.append(f"{mark(s.get('in_sync', True))}skill {k}: {s['path']}"
                             + ("" if s.get("owned", True) else " (not LabPilot-owned)"))
        lines.append(f"{mark(r['browser']['available'])}headless Chromium: {r['browser'].get('chromium') or r['browser'].get('fix')}")
        lines.append(f"{mark(r['local_kernel']['available'])}local Jupyter kernel")
        cc = r["colab_cli"]
        lines.append(f"{mark(cc.get('authenticated'))}colab-cli: {cc.get('version') or cc.get('detail', '')}"
                     + ("" if cc.get("authenticated") or not cc.get("version") else " (not signed in)"))
        lines.append(f"{mark(r['colab_mcp']['registered'])}colab-mcp: {r['colab_mcp']['detail']}")
        lm = r["nuts_lms"]
        lines.append(f"{mark(lm['ready'])}Nuts LMS MCP (optional, new-lab checks): "
                     + ("ready" if lm["ready"] else "") + (f" next: {lm['fix']}" if lm["fix"] else ""))
        pr = r["page_renderer"]
        lines.append(f"{mark(pr['available'])}Word page rendering (optional): {pr['kind'] or 'none'}")
        lines.append(f"ready: colab={r['ready_for_colab']} local fallback={r['ready_for_local_fallback']}")
        lines += [f"!! {p}" for p in r["problems"]]
        return "\n".join(lines)

    _run(lambda: run_doctor(Path(project_dir)), json_out, human)


@app.command()
def setup(
    agent: str = typer.Option("codex", help="codex, claude or both"),
    backend: str = typer.Option("auto", help="auto, local, colab-cli or colab-mcp"),
    scope: str = typer.Option("project", help="project (this folder only) or user (all projects; explicit opt-in)"),
    project_dir: Path = typer.Option(Path.cwd, "--project-dir"),  # noqa: B008
    register_mcp: bool = typer.Option(False, help="Register the official colab-mcp server with Claude Code"),
    install_colab_cli: bool = typer.Option(False, help="Install the official google-colab-cli into a "
                                           "LabPilot-owned environment (explicit opt-in)"),
    json_out: bool = JSON_OPT,
) -> None:
    """Install the LabPilot skill for your agent and record backend preferences (rerunnable, no duplicates)."""
    from labpilot.agents import install_skill, register_colab_mcp, skill_targets
    from labpilot.config import load_config, save_config

    def do() -> dict[str, Any]:
        if backend not in ("auto", "local", "colab-cli", "colab-mcp"):
            raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, f"Unknown backend '{backend}'.", exit_code=ExitCode.CONFIG)
        if scope not in ("project", "user"):
            raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, "scope must be project or user", exit_code=ExitCode.CONFIG)
        colab = None
        if install_colab_cli:
            from labpilot.colab_setup import install_colab_cli as do_install

            colab = do_install()  # writes its command into the user config before we load it below
        cfg = load_config()
        installed = [install_skill(t) for t in skill_targets(agent, scope, Path(project_dir))]
        cfg["backend"] = backend
        cfg["agents"] = sorted(set(cfg.get("agents", [])) | ({"codex", "claude"} if agent == "both" else {agent}))
        cfg["installed_skills"] = sorted(set(cfg.get("installed_skills", [])) | {i["path"] for i in installed})
        mcp = None
        if register_mcp:
            mcp = [register_colab_mcp(a) for a in (["codex", "claude"] if agent == "both" else [agent])]
            for m in mcp:
                if m.get("owned"):
                    cfg["registered_mcp"] = [e for e in cfg.get("registered_mcp", []) if e.get("agent") != m["agent"]] + [m]
        path = save_config(cfg)
        notes = ["Restart your agent (or reload its skills) so it discovers the LabPilot skill.",
                 "Then ask: Use LabPilot to work through the lab in this folder ..."]
        if backend in ("colab-cli", "auto") and not cfg["colab_cli"].get("command"):
            notes.append("Google Colab CLI not configured yet: rerun with --install-colab-cli, then `labpilot colab-login`.")
        if colab and colab.get("native_windows"):
            notes.append("Upstream google-colab-cli lists Linux/macOS only; LabPilot runs it natively on Windows "
                         "(verified on Windows 11 with CLI 0.8.0). If it misbehaves, report it and use --backend local.")
        if colab:
            notes.append(colab["next"])
        if backend == "colab-mcp" and not register_mcp:
            notes.append("colab-mcp needs registration in Claude Code: rerun with --register-mcp --agent claude.")
        return {"skills": installed, "config": str(path), "backend": backend, "colab_cli": colab, "mcp": mcp,
                "notes": notes}

    _run(do, json_out)


@app.command("colab-login")
def colab_login() -> None:
    """Sign in to Google Colab once with the official CLI flow (interactive; run it in your own terminal)."""
    from labpilot.colab_setup import interactive_login

    try:
        code = interactive_login()
    except LabPilotError as exc:
        sys.stderr.write(f"error [{exc.code}]: {exc.message}\nnext: {exc.next_action}\n")
        raise typer.Exit(int(exc.exit_code)) from None
    raise typer.Exit(code)


@app.command()
def uninstall(
    agent: str = typer.Option("both"), scope: str = typer.Option("project"),
    project_dir: Path = typer.Option(Path.cwd, "--project-dir"),  # noqa: B008
    json_out: bool = JSON_OPT,
) -> None:
    """Remove LabPilot-owned skill copies and MCP entries only."""
    from labpilot.agents import skill_targets, uninstall_skill, unregister_colab_mcp
    from labpilot.config import load_config, save_config

    def do() -> dict[str, Any]:
        cfg = load_config()
        removed = [uninstall_skill(t) for t in skill_targets(agent, scope, Path(project_dir))]
        cfg["installed_skills"] = [p for p in cfg.get("installed_skills", []) if not any(r["path"] == p and r["removed"] for r in removed)]
        mcp = [unregister_colab_mcp(e) for e in cfg.get("registered_mcp", [])]
        cfg["registered_mcp"] = []
        save_config(cfg)
        return {"skills": removed, "mcp": mcp}

    _run(do, json_out)


# ---------------------------------------------------------------- run lifecycle


@app.command()
def init(
    lab: Path = typer.Option(..., "--lab", help="The lab manual (.docx)."),  # noqa: B008
    data_dir: Path = typer.Option(None, "--data-dir", help="Lab folder with the datasets (default: the manual's folder)."),  # noqa: B008
    student_name: str = typer.Option(None, help="Fill the student name field (only if you supply it)."),
    student_id: str = typer.Option(None, help="Fill the CMS/registration field (only if you supply it)."),
    synthetic: bool = typer.Option(False, help="Label this run as a synthetic demo (never the real assignment)."),
    json_out: bool = JSON_OPT,
) -> None:
    """Create an isolated run: hash and copy the manual; return the run id."""
    from labpilot.orchestration import init_run, inspect_run

    def do() -> dict[str, Any]:
        run = init_run(lab, data_dir, {"name": student_name, "id": student_id}, synthetic)
        with run.lock():
            summary = inspect_run(run)
        return {"run_id": run.id, "run_dir": str(run.dir), "source_sha256": run.manifest.source_sha256,
                "inspection": summary, "next": f"labpilot inspect {run.id} --json  (then write plan.json)"}

    _run(do, json_out)


@app.command("colab-check")
def colab_check(file: Path, json_out: bool = JSON_OPT) -> None:
    """Decide whether a lab manual can be done in Google Colab: a .docx, a saved `read_document` result (.json), or text."""
    from labpilot.documents.inspect import colab_fit, inspect_docx
    from labpilot.lms import load_tool_json

    def do() -> dict[str, Any]:
        if file.suffix.lower() == ".docx":
            text = "\n".join(b.text for b in inspect_docx(file, "").blocks)
        elif file.suffix.lower() == ".json":
            text = str(load_tool_json(file, "read_document").get("text", ""))
        else:
            text = file.read_text(encoding="utf-8-sig")
        return {"file": file.name, **colab_fit(text)}

    _run(do, json_out, lambda r: f"{'Colab: yes' if r['colab_ok'] else 'Colab: NO'} - {r['reason']}")


@app.command()
def inspect(run_id: str, alias: list[str] = typer.Option(None, "--alias", help="EXPECTED=ACTUAL dataset mapping"),  # noqa: B008
            lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Extract document structure (anchors, tasks, solution boxes, questions) and dataset metadata."""
    from labpilot.orchestration import inspect_run

    aliases = {}
    for a in alias or []:
        if "=" not in a:
            raise typer.BadParameter("--alias must be EXPECTED=ACTUAL")
        k, v = a.split("=", 1)
        aliases[k.strip()] = v.strip()
    _run(_locked(run_id, lab_dir, lambda r: inspect_run(r, aliases)), json_out)


@app.command()
def plan(run_id: str, file: Path = typer.Option(..., "--file"), allow_uncovered: bool = False,  # noqa: B008
         lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Validate and save the agent's structured plan."""
    from labpilot.orchestration import save_plan

    _run(_locked(run_id, lab_dir, lambda r: save_plan(r, file, allow_uncovered)), json_out)


@app.command("build-notebook")
def build_notebook(run_id: str, file: Path = typer.Option(..., "--file"),  # noqa: B008
                   approve: list[str] = typer.Option(None, "--approve", help="Security rule id approved by the user"),  # noqa: B008
                   lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Build the source notebook from agent-generated cells (screened by the security policy)."""
    from labpilot.orchestration import build

    _run(_locked(run_id, lab_dir, lambda r: build(r, file, set(approve or []))), json_out)


@app.command()
def preflight(run_id: str, backend: str = typer.Option("auto", help="auto (Colab if configured), colab-cli, "
                                                                     "colab-mcp or local"),
              allow_cloud: bool = typer.Option(False, help="User consents to allocating cloud (Colab) compute."),
              allow_upload: bool = typer.Option(False, help="User consents to uploading the datasets to the cloud runtime."),
              lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Validate data, dependencies, backend and permissions; stage inputs."""
    from labpilot.orchestration import preflight as do_pf

    _run(_locked(run_id, lab_dir, lambda r: do_pf(r, backend, allow_cloud, allow_upload)),
         json_out, incomplete=lambda res: not res.get("ok"))


@app.command()
def execute(run_id: str, cell: str = typer.Option(None, "--cell", help="Replay the prefix up to and including this cell."),
            timeout: int = typer.Option(None, help="Per-cell timeout in seconds."),
            retry_budget: int = typer.Option(None, help="Override the per-cell repair budget."),
            backend: str = typer.Option(None, help="Override the backend chosen at preflight."),
            lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Run planned cells in order in one persistent kernel (fresh kernel per attempt; prefix replay)."""
    from labpilot.orchestration import execute as do_exec

    def incomplete(res: dict[str, Any]) -> bool:
        return res.get("status") != "OK"

    _run(_locked(run_id, lab_dir, lambda r: do_exec(r, cell, timeout, retry_budget, backend)), json_out,
         incomplete=incomplete)


@app.command("record-execution")
def record_execution(run_id: str, file: Path = typer.Option(..., "--file"),  # noqa: B008
                     lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Ingest verified host-mediated (colab-mcp) execution results."""
    from labpilot.orchestration import record_execution as do_rec

    _run(_locked(run_id, lab_dir, lambda r: do_rec(r, file)), json_out, incomplete=lambda res: res.get("status") != "OK")


@app.command()
def capture(run_id: str, mode: str = typer.Option("auto", help="auto/rendered (local rendering) or native"),
            lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Capture real PNG evidence of the executed notebook."""
    from labpilot.orchestration import capture as do_cap

    _run(_locked(run_id, lab_dir, lambda r: do_cap(r, "native" if mode == "native" else "auto")), json_out,
         incomplete=lambda res: bool(res.get("problems")))


@app.command()
def answers(run_id: str, file: Path = typer.Option(..., "--file"),  # noqa: B008
            lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Validate and persist grounded written answers."""
    from labpilot.orchestration import save_answers

    _run(_locked(run_id, lab_dir, lambda r: save_answers(r, file)), json_out)


@app.command()
def export(run_id: str, no_render: bool = typer.Option(False, help="Skip optional LibreOffice page rendering."),
           lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Write the Word copy, executed notebook, screenshots, report and REVIEW.md."""
    from labpilot.report import export as do_export

    def human(r: dict[str, Any]) -> str:
        return _final_status(r["run_id"], lab_dir)

    _run(_locked(run_id, lab_dir, lambda r: do_export(r, not no_render)), json_out, human,
         incomplete=lambda res: res.get("status") != "COMPLETED")


@app.command()
def validate(run_id: str, lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Check completeness, provenance and document structure."""
    from labpilot.report import validate_run

    _run(lambda: validate_run(_open(run_id, lab_dir)), json_out, incomplete=lambda res: not res.get("ok"))


@app.command()
def clean(run_id: str = typer.Argument(None, help="Run to clean (omit with --all)."),
          all_runs: bool = typer.Option(False, "--all", help="Clean every COMPLETED run in the lab folder."),
          force: bool = typer.Option(False, help="Also clean a run that is not COMPLETED (it can no longer resume)."),
          lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Free the workspace of a COMPLETED lab; keep the Word file, notebook, screenshots and reports."""
    from labpilot.cleanup import clean_run
    from labpilot.models import RunState
    from labpilot.workspace import Run, runs_root

    def do() -> dict[str, Any]:
        if run_id:
            run = _open(run_id, lab_dir)
            with run.lock():
                return clean_run(run, force)
        if not all_runs:
            raise LabPilotError(ErrorCode.RUN_NOT_FOUND, "Give a run id or --all.")
        results, skipped = [], []
        root = runs_root(lab_dir or Path.cwd())
        for p in sorted(root.iterdir()) if root.exists() else []:
            if not (p / "manifest.json").exists():
                continue
            run = Run(p)
            if run.manifest.state == RunState.COMPLETED or force:
                with run.lock():
                    results.append(clean_run(run, force))
            else:
                skipped.append({"run_id": run.id, "state": str(run.manifest.state)})
        return {"cleaned": results, "skipped_not_completed": skipped,
                "freed_mb": round(sum(r["freed_bytes"] for r in results) / 1e6, 1)}

    _run(do, json_out)


@app.command("evaluate")
def evaluate_cmd(run_id: str, lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Quality review: prose answers, clean outputs, filled tables, evidence, covered sections."""
    from labpilot.evaluate import evaluate

    def do() -> dict[str, Any]:
        run = _open(run_id, lab_dir)
        out = run.manifest.outputs
        path = Path(out["dir"], out["docx"]) if out.get("docx") else None
        return evaluate(run, path)

    _run(do, json_out, incomplete=lambda res: not res.get("ok"))


def _final_status(run_id: str, lab_dir: Path | None) -> str:
    from labpilot.report import status as st

    s = st(_open(run_id, lab_dir))
    tasks = s["tasks"]
    passed = sum(1 for v in tasks.values() if v == "passed")
    out = s.get("outputs") or {}
    d = Path(out["dir"]) if out.get("dir") else None
    lines = [f"LabPilot | run {s['run_id']}" + ("  [SYNTHETIC DEMO]" if s["synthetic_demo"] else ""),
             f"Backend: {s['executed_on'] or s['backend']} | Evidence: rendered executed notebook (local rendering)",
             f"Tasks verified: {passed}/{len(tasks)}", f"Status: {s['state']}"]
    if d:
        lines += [f"Word: {d / out['docx']}", f"Notebook: {d / out['notebook']}", f"Report: {d / out['report']}",
                  f"Review: {d / out['review']}"]
    for b in s["blockers"]:
        lines.append(f"Blocker: {b['code']} — {b['message']}")
    na = s["next_action"]
    if na["action"] != "done":
        lines.append(f"Next ({na['actor']}): {na['action']} — {na['detail']}")
    return "\n".join(lines)


@app.command()
def status(run_id: str = typer.Argument(None), lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Show progress, blockers, failures and artifact paths (latest run if no id)."""
    from labpilot.report import status as st
    from labpilot.workspace import latest_run

    def do() -> dict[str, Any]:
        run = _open(run_id, lab_dir) if run_id else latest_run(lab_dir or Path.cwd())
        if run is None:
            raise LabPilotError(ErrorCode.RUN_NOT_FOUND, "No LabPilot runs in this folder.")
        return st(run)

    _run(do, json_out, lambda r: _final_status(r["run_id"], lab_dir))


@app.command()
def resume(run_id: str, backend: str = typer.Option(None), allow_cloud: bool = False, allow_upload: bool = False,
           lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Continue from the latest consistent checkpoint; report the next agent/user action."""
    from labpilot.report import resume as do_resume

    _run(_locked(run_id, lab_dir, lambda r: do_resume(r, backend, allow_cloud, allow_upload)), json_out,
         incomplete=lambda res: res["state"] != "COMPLETED")


@app.command()
def cancel(run_id: str, lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """Safely stop LabPilot-owned processes/sessions for a run and persist its state."""
    from labpilot.orchestration import cancel as do_cancel

    _run(lambda: do_cancel(_open(run_id, lab_dir)), json_out)


@app.command()
def runs(lab_dir: Path = LAB_DIR_OPT, json_out: bool = JSON_OPT) -> None:
    """List runs in a lab folder."""
    from labpilot.workspace import Run, runs_root

    def do() -> dict[str, Any]:
        root = runs_root(lab_dir or Path.cwd())
        items = []
        if root.exists():
            for p in sorted(root.iterdir()):
                if (p / "manifest.json").exists():
                    m = Run(p).manifest
                    items.append({"run_id": m.run_id, "state": str(m.state), "source": m.source_name,
                                  "updated_at": m.updated_at, "synthetic": m.synthetic})
        return {"runs": items}

    _run(do, json_out)


# ---------------------------------------------------------------- LMS (Nuts LMS MCP, host-mediated)

lms_app = typer.Typer(no_args_is_help=True, help="Find new labs on the LMS via the Nuts LMS MCP (moodle-mcp). The host "
                      "agent calls the MCP tools and passes their JSON here; LabPilot never logs in to the LMS.")
app.add_typer(lms_app, name="lms")
SITE_OPT = typer.Option(None, "--site", help="LMS base URL, to keep labs of different sites apart (optional).")


@lms_app.command("check")
def lms_check(
    assignments: Path = typer.Option(..., "--assignments", help="Saved JSON output of the `list_assignments` tool."),  # noqa: B008
    status_files: list[Path] = typer.Option(None, "--status", help="Saved `assignment_status` JSON (repeatable)."),  # noqa: B008
    match: str = typer.Option(None, "--match", help="Regex for lab names (default: names containing 'lab')."),
    include_past: bool = typer.Option(False, "--include-past", help="Also list labs whose deadline has passed."),
    root: str = typer.Option(None, "--root", help="Base folder for suggested lab folders (default: lms-labs)."),
    site: str = SITE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """List new labs: lab-like name, still open, not submitted, not yet downloaded/processed/ignored."""
    from labpilot import lms

    def do() -> dict[str, Any]:
        statuses = [lms.load_tool_json(p, "assignment_status") for p in status_files or []]
        return lms.check_labs(lms.load_tool_json(assignments, "list_assignments"), statuses, site=site,
                              pattern=match or lms.DEFAULT_LAB_PATTERN, include_past=include_past,
                              root=root or lms.DEFAULT_ROOT)

    def human(r: dict[str, Any]) -> str:
        c = r["counts"]
        lines = [f"{c['new']} new lab(s) · {c['in_progress']} in progress · {c['handled']} handled · "
                 f"{c['submitted']} submitted · {c['closed']} closed · {c['not_labs']} other assignments"]
        for lab in r["new_labs"]:
            flags = [lab["submission_status"]] + (["overdue"] if lab["overdue"] else []) + \
                    (["not open yet"] if lab["not_open_yet"] else [])
            lines.append(f"  [{lab['assignid']}] {lab['course']}: {lab['name']}  due {lab['duedate'] or '-'}  "
                         f"({', '.join(flags)})")
        for lab in r["in_progress"]:
            lines.append(f"  in progress [{lab['assignid']}] {lab['name']} -> {lab['folder']}")
        lines.append(f"next: {r['next']}")
        return "\n".join(lines)

    _run(do, json_out, human)


@lms_app.command("stage")
def lms_stage(
    folder: Path = typer.Argument(..., help="Lab folder the agent filled with the MCP `download_file` tool."),  # noqa: B008
    assignments: Path = typer.Option(..., "--assignments", help="Saved `list_assignments` JSON."),  # noqa: B008
    assign_id: int = typer.Option(..., "--assign-id", help="assignid of this lab."),
    source_url: list[str] = typer.Option(None, "--source-url", help="Moodle fileurl a file came from (repeatable)."),  # noqa: B008
    site: str = SITE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Record where a downloaded lab came from (lms_source.json) and mark it in progress."""
    from labpilot import lms

    _run(lambda: lms.stage_lab(folder, lms.load_tool_json(assignments, "list_assignments"), assign_id, site=site,
                               source_urls=source_url or []), json_out)


@lms_app.command("mark")
def lms_mark(
    assign_id: int = typer.Argument(..., help="assignid of the lab."),
    state: str = typer.Option(..., "--state", help="processed (run completed), ignored (user skipped) or downloaded."),
    run_id: str = typer.Option(None, "--run-id", help="LabPilot run that completed this lab."),
    name: str = typer.Option(None, "--name", help="Lab name, for the ledger listing."),
    site: str = SITE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Record a lab as processed or ignored so `lms check` stops reporting it."""
    from labpilot import lms

    _run(lambda: lms.mark_lab(assign_id, state, site=site, run_id=run_id, name=name), json_out)


@lms_app.command("list")
def lms_list(json_out: bool = JSON_OPT) -> None:
    """Show the labs LabPilot has recorded (downloaded, processed or ignored)."""
    from labpilot import lms

    _run(lms.list_ledger, json_out)


def main() -> None:
    if sys.platform == "win32":
        import contextlib

        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    app()


if __name__ == "__main__":
    main()
