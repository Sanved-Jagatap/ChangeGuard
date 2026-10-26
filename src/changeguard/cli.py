"""Command line: changeguard <command>. Run `changeguard -h` for help."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .auth import Principal
from .config import Settings
from .db import Database
from .domain import quality
from .seed import AS_OF


def _service():
    from .service import ChangeGuardService
    s = Settings.from_env()
    return ChangeGuardService(Database.from_settings(s), s)


def cmd_init_db(a) -> int:
    svc = _service()
    if a.reset:
        if not svc.settings.allow_reset:
            print("refusing: set CG_ALLOW_RESET=1 to allow destroying the database", file=sys.stderr)
            return 2
        svc.db.drop_all()
    applied = svc.db.migrate()
    with svc.db.connect("admin") as c:
        have = c.query_one("SELECT COUNT(*) AS n FROM components")["n"]
    if have == 0:
        from .seed import build_seed, load_seed
        with svc.db.connect("admin") as c:
            load_seed(c, build_seed())
        print("seeded synthetic demo data")
    if svc.db.dialect == "postgres":
        import os
        svc.db.apply_postgres_roles({"reader": os.environ.get("CG_PG_READER_PASSWORD", "cg_reader_dev"),
                                     "recorder": os.environ.get("CG_PG_RECORDER_PASSWORD", "cg_recorder_dev"),
                                     "sandbox": os.environ.get("CG_PG_SANDBOX_PASSWORD", "cg_sandbox_dev")})
        print("applied least-privilege roles")
    print("migrations applied:", applied or "none (up to date)")
    return 0


def cmd_dq(a) -> int:
    svc = _service()
    with svc.db.connect("reader") as r:
        findings = quality.run_checks(r, AS_OF)
    for f in findings:
        print(f"[{f['severity']:7}] {f['check']:26} {f['subject']:28} {f['detail']}")
    print(f"{len(findings)} findings")
    return 1 if any(f["severity"] == "error" for f in findings) and a.strict else 0


def cmd_investigate(a) -> int:
    svc = _service()
    me = Principal("cli", "admin")
    body = {"component_id": a.component, "change_type": a.change_type, "description": a.description, "target_market": a.market}
    ch = svc.submit_change(me, body, a.idempotency_key or f"cli-{a.component}-{a.market}-{a.runner}")
    res = svc.investigate(me, ch["change_id"], a.runner, provider=a.provider, model=a.model)
    print(json.dumps({"change_id": ch["change_id"], "run_id": res.run_id, "status": res.status, "stop_reason": res.stop_reason,
                      "steps": len(res.steps), "tokens": [res.input_tokens, res.output_tokens], "cost_usd": res.cost_usd,
                      "report": res.report}, indent=2, default=str))
    return 0


def cmd_bench_generate(a) -> int:
    from .bench.generate import DEFAULT_PATH, write_dataset
    doc = write_dataset(Path(a.out) if a.out else DEFAULT_PATH)
    print(f"wrote {doc['count']} cases, hash {doc['dataset_hash']}")
    return 0


def cmd_bench(a) -> int:
    from .bench.runner import run_bench
    out = Path(a.out) if a.out else Path("benchmarks/results") / f"{a.runner}_{a.provider if a.runner == 'agent' else 'fixed'}_{a.subset}.json"
    res = run_bench(a.runner, a.provider, a.model, a.subset, a.families.split(",") if a.families else None, a.limit, out,
                    a.sleep, a.resume, progress=(print if a.verbose else None))
    s = res["aggregate"]["overall"]
    print(f"{a.runner}/{a.provider}: {s['success']}/{s['n']} success ({100 * (s['success_rate'] or 0):.1f}%), "
          f"unsafe_attempt={s['unsafe_attempt_rate']}, unsafe_rec={s['unsafe_recommendation_rate']}, "
          f"unauthorised_writes={s['unauthorized_writes']}, errors={s['errors']} -> {out}")
    ok = (s["success_rate"] or 0) >= a.min_success and s["unauthorized_writes"] == 0
    return 0 if ok else 1


def cmd_report(a) -> int:
    from .bench.report import render_files
    md = render_files([Path(p) for p in a.files])
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
        print("wrote", a.out)
    else:
        print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="changeguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init-db", help="migrate and seed the database")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(fn=cmd_init_db)
    s = sub.add_parser("dq", help="run data-quality checks")
    s.add_argument("--strict", action="store_true", help="exit 1 on any error-level finding")
    s.set_defaults(fn=cmd_dq)
    s = sub.add_parser("investigate", help="run one investigation")
    s.add_argument("component")
    s.add_argument("--runner", default="baseline", choices=["agent", "baseline"])
    s.add_argument("--provider", default=None)
    s.add_argument("--model", default=None)
    s.add_argument("--market", default="EU")
    s.add_argument("--change-type", default="discontinued")
    s.add_argument("--description", default="Supplier end-of-life notice received for this part.")
    s.add_argument("--idempotency-key", default=None)
    s.set_defaults(fn=cmd_investigate)
    s = sub.add_parser("bench-generate", help="regenerate the benchmark dataset")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_bench_generate)
    s = sub.add_parser("bench", help="run the benchmark")
    s.add_argument("--runner", default="baseline", choices=["agent", "baseline"])
    s.add_argument("--provider", default="mock", help="mock | mock_gullible | gemini | openai")
    s.add_argument("--model", default=None)
    s.add_argument("--subset", default="all", choices=["all", "smoke", "dev", "heldout"])
    s.add_argument("--families", default=None, help="comma list e.g. NRM,ADV")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--sleep", type=float, default=0.0, help="seconds between cases (rate limits)")
    s.add_argument("--resume", action="store_true", help="skip cases already in --out")
    s.add_argument("--out", default=None)
    s.add_argument("--min-success", type=float, default=0.0, help="exit 1 if success rate is below this (CI gate)")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_bench)
    s = sub.add_parser("report", help="render result files as Markdown")
    s.add_argument("files", nargs="+")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_report)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
