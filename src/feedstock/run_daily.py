"""
run_daily.py — orchestrate the full daily pipeline, in order, as one command.

This is the single entry point the GitHub Actions workflow (and you, locally)
invoke. It runs the four phases in dependency order:

    ingest    -> pull fresh observations into SQLite
    analysis  -> compute & persist the margin-pressure index from them
    narrative -> generate the AI/templated paragraph for the day
    alert     -> evaluate the threshold, flag, and maybe email

Each step is wrapped so a failure in one doesn't abort the rest: if ingest
can't reach a source, analysis/narrative/alert can still run on the history
already in the DB. We collect failures and exit non-zero if any step raised,
so CI surfaces the problem — but because the workflow commits the DB in an
`if: always()` step, whatever data we *did* gather is still persisted.
"""

from __future__ import annotations

import importlib
import sys
import traceback

# (label, module name, function to call)
STEPS = [
    ("ingest", "ingest", "run_ingest"),
    ("analysis", "analysis", "run_analysis"),
    ("narrative", "narrative", "run_narrative"),
    ("alert", "alerting", "run_alert_check"),
    ("backtest", "backtest", "run_backtest_cli"),
]


def main() -> int:
    failures: list[str] = []
    for label, module_name, fn_name in STEPS:
        print("\n" + "#" * 64)
        print(f"# STEP: {label}")
        print("#" * 64)
        try:
            module = importlib.import_module(f"feedstock.{module_name}")
            getattr(module, fn_name)()
        except Exception:  # noqa: BLE001 — isolate steps; keep the run going
            failures.append(label)
            print(f"[run_daily] step '{label}' FAILED:")
            traceback.print_exc()

    print("\n" + "=" * 64)
    if failures:
        print(f"[run_daily] completed WITH FAILURES: {', '.join(failures)}")
        return 1
    print("[run_daily] all steps completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
