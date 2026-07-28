#!/usr/bin/env python3
"""Seed a demo case into a running GoLogs instance (PRD §28).

Combines two data sources:
  1. The same real `.evtx` fixtures the test suite validates against
     (sourced from python-evtx's own GitHub test corpus -- see
     `docs/context_transfer.md` §3 for provenance) -- real EVTX binary
     structure, ~3,860 ordinary Security/System events.
  2. A small, hand-scripted "suspicious PowerShell + persistence via
     scheduled task" scenario (PRD §28), inserted as additional Event
     rows so a demo audience has a concrete, interesting narrative to ask
     the copilot about rather than only generic background noise.
     Entirely synthetic -- no real person, host, or organization.

Usage:
    python scripts/seed_demo_case.py [--db-url sqlite:///backend/data/copilot.db]
                                      [--case-storage-dir backend/data/cases]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
FIXTURES_DIR = BACKEND_ROOT / "tests" / "fixtures"

sys.path.insert(0, str(BACKEND_ROOT))

# Entirely synthetic -- no real person, host, or organization.
_SCENARIO_COMPUTER = "WKSTN-FINANCE07"
_SCENARIO_USER_SID = "S-1-5-21-1004336348-1177238915-682003330-1105"
_SCENARIO_USER_NAME = "j.martinez"
_SCENARIO_START = datetime(2026, 3, 14, 2, 17, 0, tzinfo=timezone.utc)  # off-hours


def _scenario_event(
    minutes_offset: int,
    channel: str,
    event_id: int,
    raw_xml: str,
    normalized_fields: dict,
) -> dict:
    return {
        "uid": str(uuid.uuid4()),
        "channel": channel,
        "event_id": event_id,
        "time_created": _SCENARIO_START + timedelta(minutes=minutes_offset),
        "computer": _SCENARIO_COMPUTER,
        "user_sid": _SCENARIO_USER_SID,
        "raw_xml": raw_xml,
        "normalized_fields": normalized_fields,
    }


def _build_scripted_scenario() -> list[dict]:
    """PRD §28's "suspicious PowerShell + persistence via scheduled task"
    demo narrative. Five events across ~7 synthetic minutes: an off-hours
    interactive logon, an obfuscated PowerShell process launch, a
    scheduled-task persistence mechanism disguised with an Edge-updater-
    style name, a Run-key registry persistence backstop, and a logoff."""
    return [
        _scenario_event(
            0,
            "Security",
            4624,
            f'<Event><System><Channel>Security</Channel><EventID>4624</EventID></System>'
            f'<EventData><Data Name="TargetUserName">{_SCENARIO_USER_NAME}</Data>'
            f'<Data Name="TargetUserSid">{_SCENARIO_USER_SID}</Data>'
            f'<Data Name="LogonType">10</Data>'
            f'<Data Name="IpAddress">203.0.113.44</Data></EventData></Event>',
            {
                "provider": "Microsoft-Windows-Security-Auditing",
                "data": {
                    "TargetUserName": _SCENARIO_USER_NAME,
                    "TargetUserSid": _SCENARIO_USER_SID,
                    "LogonType": "10",
                    "IpAddress": "203.0.113.44",
                },
                "scenario_note": "Interactive RDP logon (LogonType 10) at 02:17 UTC, well outside business hours.",
            },
        ),
        _scenario_event(
            2,
            "Security",
            4688,
            f'<Event><System><Channel>Security</Channel><EventID>4688</EventID></System>'
            f'<EventData><Data Name="NewProcessName">C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Data>'
            f'<Data Name="CommandLine">powershell.exe -NoP -W Hidden -Enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABOAGUAdAAuAFcAZQBiAEMAbABpAGUAbgB0ACkALgBEAG8AdwBuAGwAbwBhAGQAUwB0AHIAaQBuAGcA</Data>'
            f'<Data Name="SubjectUserName">{_SCENARIO_USER_NAME}</Data></EventData></Event>',
            {
                "provider": "Microsoft-Windows-Security-Auditing",
                "data": {
                    "NewProcessName": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                    "CommandLine": "powershell.exe -NoP -W Hidden -Enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABOAGUAdAAuAFcAZQBiAEMAbABpAGUAbgB0ACkALgBEAG8AdwBuAGwAbwBhAGQAUwB0AHIAaQBuAGcA",
                    "SubjectUserName": _SCENARIO_USER_NAME,
                },
                "scenario_note": "Base64-encoded, hidden-window PowerShell -- a classic obfuscated download-cradle pattern.",
            },
        ),
        _scenario_event(
            4,
            "Microsoft-Windows-TaskScheduler/Operational",
            4698,
            f'<Event><System><Channel>Microsoft-Windows-TaskScheduler/Operational</Channel><EventID>4698</EventID></System>'
            f'<EventData><Data Name="TaskName">\\Microsoft\\Windows\\EdgeUpdate\\MicrosoftEdgeUpdateTaskMachineUA2</Data>'
            f'<Data Name="UserContext">{_SCENARIO_USER_NAME}</Data>'
            f'<Data Name="ActionExecutable">C:\\Users\\Public\\upd.exe</Data></EventData></Event>',
            {
                "provider": "Microsoft-Windows-TaskScheduler",
                "data": {
                    "TaskName": "\\Microsoft\\Windows\\EdgeUpdate\\MicrosoftEdgeUpdateTaskMachineUA2",
                    "UserContext": _SCENARIO_USER_NAME,
                    "ActionExecutable": "C:\\Users\\Public\\upd.exe",
                },
                "scenario_note": "Scheduled task name mimics the real Edge updater task, but its action executable "
                "lives in C:\\Users\\Public -- not a legitimate Edge install path. Likely persistence.",
            },
        ),
        _scenario_event(
            5,
            "Security",
            4657,
            f'<Event><System><Channel>Security</Channel><EventID>4657</EventID></System>'
            f'<EventData><Data Name="ObjectName">\\REGISTRY\\MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run</Data>'
            f'<Data Name="ObjectValueName">SecurityHealthUpd</Data>'
            f'<Data Name="NewValue">C:\\Users\\Public\\upd.exe</Data>'
            f'<Data Name="SubjectUserName">{_SCENARIO_USER_NAME}</Data></EventData></Event>',
            {
                "provider": "Microsoft-Windows-Security-Auditing",
                "data": {
                    "ObjectName": "\\REGISTRY\\MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
                    "ObjectValueName": "SecurityHealthUpd",
                    "NewValue": "C:\\Users\\Public\\upd.exe",
                    "SubjectUserName": _SCENARIO_USER_NAME,
                },
                "scenario_note": "A second, redundant persistence mechanism (Run key) pointing at the same "
                "C:\\Users\\Public\\upd.exe binary as the scheduled task.",
            },
        ),
        _scenario_event(
            7,
            "Security",
            4634,
            f'<Event><System><Channel>Security</Channel><EventID>4634</EventID></System>'
            f'<EventData><Data Name="TargetUserName">{_SCENARIO_USER_NAME}</Data>'
            f'<Data Name="TargetUserSid">{_SCENARIO_USER_SID}</Data></EventData></Event>',
            {
                "provider": "Microsoft-Windows-Security-Auditing",
                "data": {"TargetUserName": _SCENARIO_USER_NAME, "TargetUserSid": _SCENARIO_USER_SID},
                "scenario_note": "Logoff, closing the ~7-minute session.",
            },
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=f"sqlite:///{BACKEND_ROOT / 'data' / 'copilot.db'}")
    parser.add_argument(
        "--case-storage-dir", default=str(BACKEND_ROOT / "data" / "cases"), type=Path
    )
    parser.add_argument("--case-name", default="Demo: Suspicious Off-Hours Access")
    args = parser.parse_args()

    from app.api.routes_cases import ingest_case_files
    from app.core import fs_sandbox
    from app.core.models import Case, Event
    from app.db.session import init_db, make_engine, make_session_factory, session_scope

    fixture_files = [FIXTURES_DIR / "sample_security.evtx", FIXTURES_DIR / "sample_system.evtx"]
    missing = [f for f in fixture_files if not f.exists()]
    if missing:
        print(f"Missing fixture file(s): {missing}. Run this from the repo root.")
        return 1

    engine = make_engine(args.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    case_storage_dir = Path(args.case_storage_dir)
    case_storage_dir.mkdir(parents=True, exist_ok=True)

    with session_scope(session_factory) as session:
        case = Case(name=args.case_name, status="created")
        session.add(case)
        session.flush()
        case_id = case.id

    fs_sandbox.ensure_case_dir(case_storage_dir, case_id)
    saved_filenames = []
    for fixture in fixture_files:
        dest = fs_sandbox.resolve_case_path(case_storage_dir, case_id, fixture.name)
        shutil.copy(fixture, dest)
        saved_filenames.append(dest.name)

    print(f"Ingesting {len(saved_filenames)} background fixture file(s) for case {case_id}...")
    ingest_case_files(case_id, saved_filenames, session_factory, case_storage_dir)

    scenario_events = _build_scripted_scenario()
    with session_scope(session_factory) as session:
        for fields in scenario_events:
            session.add(Event(case_id=case_id, **fields))
    print(f"Inserted {len(scenario_events)} scripted scenario event(s).")

    with session_factory() as session:
        case = session.get(Case, case_id)
        event_count = session.query(Event).filter(Event.case_id == case_id).count()
        print(f"\nDemo case ready: {case_id}")
        print(f"  Name:   {case.name}")
        print(f"  Status: {case.status}")
        print(f"  Events: {event_count}")
        print(
            "\nTry asking the copilot: 'Was there any suspicious activity involving "
            f"{_SCENARIO_USER_NAME} or scheduled tasks?'"
        )
        print("Open the frontend and enter this case ID under 'Existing case ID'.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
