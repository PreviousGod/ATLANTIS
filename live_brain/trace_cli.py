from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from live_brain.turn_trace import TurnTraceManager  # type: ignore
    from live_brain.incident_truth import IncidentTruthManager  # type: ignore
else:
    from .turn_trace import TurnTraceManager
    from .incident_truth import IncidentTruthManager


def _db_path(hermes_home: str | None = None) -> Path:
    base = Path(hermes_home or (Path.home() / ".hermes"))
    return base / "live_brain" / "live_brain.db"


def _print_timeline(traces: dict) -> None:
    print("TRACE SUMMARY")
    for line in traces.get("summary", []):
        print(f"- {line}")
    print("\nTRACE TIMELINE")
    for item in traces.get("timeline", []):
        print(f"* {item.get('turn_kind')} tier={item.get('tier')} intent={item.get('intent')}")
        if item.get("sections"):
            print(f"  sections: {', '.join(item['sections'])}")
        if item.get("tool_name"):
            print(f"  tool: {item['tool_name']} success={item.get('success')}")
        if item.get("section_decisions"):
            for decision in item["section_decisions"][:6]:
                print(
                    f"  decision: section={decision.get('section')} allowed={decision.get('allowed')} reason={decision.get('reason')}"
                )
        if item.get("user_message"):
            print(f"  user: {item['user_message']}")
        if item.get("assistant_response"):
            print(f"  assistant: {item['assistant_response']}")
        if item.get("result_preview"):
            print(f"  result: {item['result_preview']}")


def _print_incidents(incidents: dict) -> None:
    print("\nINCIDENT TRUTHS")
    for item in incidents.get("incidents", []):
        print(f"* {item.get('title')} status={item.get('status')} confidence={item.get('confidence')}")
        print(f"  diagnosis: {item.get('diagnosis_summary')}")
        print(f"  next: {item.get('recommended_next_action')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Brain trace replay CLI")
    parser.add_argument("--session-id", default="", help="Session to replay")
    parser.add_argument("--scope-key", default="", help="Scope key to inspect")
    parser.add_argument("--query", default="", help="Optional query context")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--hermes-home", default="")
    args = parser.parse_args()

    conn = sqlite3.connect(str(_db_path(args.hermes_home)), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        traces = TurnTraceManager(conn).debug(
            args.scope_key or "global",
            args.query,
            session_id=args.session_id,
            limit=args.limit,
        )
        incidents = IncidentTruthManager(conn).debug(args.scope_key or "global", args.query)
        _print_timeline(traces)
        _print_incidents(incidents)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
