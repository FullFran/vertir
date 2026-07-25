"""VertIR command line. Zero deps (argparse).

    python -m vertir demo   --out ./out
    python -m vertir build  --hero h.mp4 --transcript t.json --out ./out [--bgm b.m4a]
                            [--plan plan.json | --baseline-plan]
    python -m vertir validate timeline.ir.json
    python -m vertir render   timeline.ir.json --out final.mp4 [--proxy]
    python -m vertir ingest   media.mp4
    python -m vertir capcut   timeline.ir.json --out ./out [--check]
    python -m vertir reconcile timeline.ir.json --draft ./out/capcut-draft \
                              --provenance ./out/capcut.provenance.json
    python -m vertir mcp                       # stdio MCP server for Claude Code
    python -m vertir web      --ir timeline.ir.json --dir ./out
"""
from __future__ import annotations

import argparse
import json
import sys

from . import ir as I
from . import probe as P
from . import transcript as T
from . import validate as V
from . import render as R


def _print_report(rep: dict) -> None:
    mark = "OK" if rep["ok"] else "FAILED"
    print(f"validation: {mark}  ({len(rep['errors'])} errors, {len(rep['warnings'])} warnings)")
    for e in rep["errors"]:
        print(f"  ERROR [{e['code']}] {e['msg']}  @{e['where']}")
    for w in rep["warnings"]:
        print(f"  warn  [{w['code']}] {w['msg']}  @{w['where']}")


def cmd_demo(a) -> int:
    from .demo import run
    res = run(a.out)
    _print_report(res["report"])
    for name, path in res["paths"].items():
        print(f"  {name}: {path}")
    return 0 if res["report"]["ok"] else 1


def cmd_build(a) -> int:
    from .pipeline import build_short
    from . import plan as PL
    tx = T.load_json(a.transcript)

    plan = None
    if a.plan:
        plan = PL.load(a.plan)
    elif a.baseline_plan:
        plan = PL.baseline_plan(tx)
    if plan is not None:
        rep = PL.validate_plan(plan, tx)
        _print_report(rep)
        if not rep["ok"]:
            return 1

    res = build_short(a.hero, tx, a.out, bgm_path=a.bgm, proxy=not a.no_proxy, plan=plan)
    _print_report(res["report"])
    for name, path in res["paths"].items():
        print(f"  {name}: {path}")
    return 0 if res["report"]["ok"] else 1


def cmd_validate(a) -> int:
    doc = I.load(a.ir)
    from . import edit as E
    E.derive(doc)
    rep = V.validate(doc)
    _print_report(rep)
    return 0 if rep["ok"] else 1


def cmd_render(a) -> int:
    doc = I.load(a.ir)
    from . import edit as E
    E.derive(doc)
    rep = V.validate(doc)
    _print_report(rep)
    if not rep["ok"]:
        return 1
    receipt = R.render(doc, a.out, proxy=a.proxy)
    print(json.dumps(receipt, indent=2))
    return 0


def cmd_ingest(a) -> int:
    aid, asset = P.ingest(a.media)
    print(json.dumps({aid: asset}, indent=2))
    return 0


def cmd_capcut(a) -> int:
    from .capcut import spec as CS
    from . import edit as E
    doc = I.load(a.ir)
    E.derive(doc)
    res = CS.export(doc, a.out, name=a.name, check_only=a.check)

    losses = res.get("losses", [])
    if losses:
        print(f"export losses ({len(losses)}):")
    for loss in losses:
        n = loss.get("count", 1)
        scope = f" x{n}" if n > 1 else ""
        print(f"  {loss['severity']:<10} [{loss['code']}]{scope} {loss['construct']}: {loss['detail']}")
    if not res["ok"]:
        print(f"\nexport failed at stage: {res['stage']}")
        if res.get("hint"):
            print(res["hint"])
        if res.get("error"):
            print(res["error"])
        if res.get("report"):
            _print_report(res["report"])
        return 1
    print(f"\nexport OK ({res['stage']})")
    for key in ("specPath", "draftPath", "provenancePath"):
        if res.get(key):
            print(f"  {key}: {res[key]}")
    return 0


def cmd_reconcile(a) -> int:
    from .capcut.reconcile import reconcile_files
    rep = reconcile_files(a.ir, a.draft, a.provenance, out_path=a.out)
    if not rep["changed"] and rep["ok"]:
        print("no changes in the draft; the IR is already up to date")
        return 0
    for item in rep.get("applied", []):
        print(f"  applied   {item['clip']}: {', '.join(item['fields'])}")
    for item in rep.get("pinnedInCapCut", []):
        who = item.get("clip") or item.get("segment")
        print(f"  pinned    {who}: {item['reason']}")
    for item in rep.get("unknownSegments", []):
        print(f"  unknown   {item.get('segment')}: {item['reason']}")
    for w in rep.get("warnings", []):
        print(f"  warn      {w}")
    for e in rep.get("errors", []):
        print(f"  ERROR     {e}")
    if rep.get("validation"):
        _print_report(rep["validation"])
    if rep["ok"]:
        print(f"\nreconciled -> {rep.get('irPath', a.ir)}")
    return 0 if rep["ok"] else 1


def cmd_mcp(a) -> int:
    from .mcp_server import serve
    serve()
    return 0


def cmd_web(a) -> int:
    from .web.app import serve
    serve(a.ir, a.dir, port=a.port)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vertir", description="AI-first vertical video editor engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo"); d.add_argument("--out", default="./vertir-out"); d.set_defaults(fn=cmd_demo)

    b = sub.add_parser("build")
    b.add_argument("--hero", required=True); b.add_argument("--transcript", required=True)
    b.add_argument("--out", default="./vertir-out"); b.add_argument("--bgm", default=None)
    b.add_argument("--no-proxy", action="store_true")
    b.add_argument("--plan", default=None, help="editorial plan JSON (hook/keep/drop/beats/emphasis)")
    b.add_argument("--baseline-plan", action="store_true",
                   help="use the deterministic baseline plan instead of the mechanical cut")
    b.set_defaults(fn=cmd_build)

    v = sub.add_parser("validate"); v.add_argument("ir"); v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("render"); r.add_argument("ir"); r.add_argument("--out", default="final.mp4")
    r.add_argument("--proxy", action="store_true"); r.set_defaults(fn=cmd_render)

    g = sub.add_parser("ingest"); g.add_argument("media"); g.set_defaults(fn=cmd_ingest)

    c = sub.add_parser("capcut", help="export an IR to a CapCut/JianYing draft")
    c.add_argument("ir"); c.add_argument("--out", default="./vertir-out")
    c.add_argument("--name", default=None); c.add_argument("--check", action="store_true")
    c.set_defaults(fn=cmd_capcut)

    rc = sub.add_parser("reconcile", help="pull CapCut edits back into the IR")
    rc.add_argument("ir"); rc.add_argument("--draft", required=True)
    rc.add_argument("--provenance", required=True)
    rc.add_argument("--out", default=None, help="write here instead of in place")
    rc.set_defaults(fn=cmd_reconcile)

    m = sub.add_parser("mcp"); m.set_defaults(fn=cmd_mcp)

    w = sub.add_parser("web"); w.add_argument("--ir", required=True); w.add_argument("--dir", default=".")
    w.add_argument("--port", type=int, default=8747); w.set_defaults(fn=cmd_web)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
