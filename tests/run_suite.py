"""Structured automated test runner classifying results as:
PASSED | FAILED | NOT TESTED | BLOCKED BY CONFIGURATION
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    total_run = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total_run - failed

    live_status = "BLOCKED BY CONFIGURATION"
    live_reason = (
        "Outbound internet / live provider credentials (NTFY_TOPIC, TELEGRAM_BOT_TOKEN) "
        "not enabled in this execution environment. Set RUN_LIVE_INTEGRATION=1 on a connected host to run."
    )
    if os.getenv("RUN_LIVE_INTEGRATION") == "1":
        live_status = "NOT TESTED"
        live_reason = "Run `python -m backend.cli run-scan` directly on a connected host to verify live Steam/CheapShark/GamerPower endpoints."

    summary = {
        "automated_unit_and_integration_tests": {
            "total": total_run,
            "PASSED": passed,
            "FAILED": failed,
        },
        "live_external_service_checks": {
            "steam_live_search_and_appdetails": {"status": live_status, "reason": live_reason},
            "cheapshark_live_api": {"status": live_status, "reason": live_reason},
            "gamerpower_live_api": {"status": live_status, "reason": live_reason},
            "ntfy_and_telegram_live_push": {"status": live_status, "reason": live_reason},
        },
        "browser_e2e_pwa_install_check": {
            "status": "NOT TESTED",
            "reason": "Requires HTTPS deployment and manual Add to Home Screen verification in Chrome on Android/Windows.",
        },
    }

    print("\n=== STRUCTURED VALIDATION SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
