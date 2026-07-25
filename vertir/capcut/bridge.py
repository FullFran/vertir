"""Gated subprocess wrapper around the `capcut` CLI.

The gate is the whole point: `capcut-cli` is a Node dependency in a project whose
core runs on stdlib Python plus ffmpeg. Probing before every call, and failing
with an actionable message instead of a stack trace, is what keeps that
dependency genuinely optional.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache

CLI = "capcut"
INSTALL_HINT = (
    "capcut-cli is not on PATH. It is an optional dependency used only for the "
    "CapCut export; the rest of the pipeline does not need it.\n"
    "  install:  npm install -g capcut-cli   (needs Node >= 18)\n"
    "  verify:   capcut doctor"
)


class CapCutUnavailable(RuntimeError):
    """Raised when the CLI is needed but not installed."""


@lru_cache(maxsize=1)
def available() -> bool:
    """True when the `capcut` CLI can be invoked. Never raises.

    Probes with `--version`, not the `version` subcommand: the latter reports a
    *draft's* schema version and needs a project path, so it exits 1 here.
    """
    if shutil.which(CLI) is None:
        return False
    try:
        p = subprocess.run([CLI, "--version"], capture_output=True, text=True, timeout=30)
        return p.returncode == 0
    except Exception:
        return False


def require() -> None:
    if not available():
        raise CapCutUnavailable(INSTALL_HINT)


def run(args: list[str], *, cwd: str | None = None, timeout: int = 300) -> dict:
    """Invoke the CLI and return {ok, stdout, stderr, json, exitCode}.

    `capcut` emits JSON by default, so `json` is the parsed payload when the
    output parses and None otherwise. Exit code 1 means "invalid input, warning,
    or operation failure" — surfaced, never swallowed.
    """
    require()
    p = subprocess.run([CLI, *args], capture_output=True, text=True,
                       cwd=cwd, timeout=timeout)
    payload = None
    if p.stdout.strip():
        try:
            payload = json.loads(p.stdout)
        except json.JSONDecodeError:
            payload = None
    return {"ok": p.returncode == 0, "exitCode": p.returncode,
            "stdout": p.stdout, "stderr": p.stderr, "json": payload,
            "argv": [CLI, *args]}


def check_error(res: dict, what: str) -> None:
    if not res["ok"]:
        detail = (res["stderr"] or res["stdout"] or "").strip()
        raise RuntimeError(f"{what} failed (exit {res['exitCode']}): {detail}")


def doctor() -> dict:
    """Environment preflight, as the CLI itself reports it."""
    if not available():
        return {"available": False, "hint": INSTALL_HINT}
    res = run(["doctor"])
    return {"available": True, "ok": res["ok"], "report": res["json"] or res["stdout"]}


def version() -> str | None:
    """The installed CLI's own version (not a draft's schema version)."""
    if not available():
        return None
    return (run(["--version"])["stdout"] or "").strip() or None


def compile_spec(spec_path: str, out_dir: str, *, check_only: bool = False,
                 plan_only: bool = False, drafts_dir: str | None = None) -> dict:
    """Run `capcut compile`. With check_only/plan_only nothing is written."""
    args = ["compile", os.path.abspath(spec_path)]
    if check_only:
        args.append("--check")
    elif plan_only:
        args.append("--plan")
    else:
        args += ["--out", os.path.abspath(out_dir)]
    if drafts_dir:
        args += ["--drafts", os.path.abspath(drafts_dir)]
    return run(args)


def lint(draft_dir: str) -> dict:
    """`capcut lint` — exit 0 clean, 1 warnings, 2 errors."""
    return run(["lint", os.path.abspath(draft_dir)])


def diff(before_dir: str, after_dir: str) -> dict:
    """`capcut diff` — the basis of the reconcile slice."""
    return run(["diff", os.path.abspath(before_dir), os.path.abspath(after_dir)])
