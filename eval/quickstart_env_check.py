"""Follow-up to F-29: the Quickstart's Manhattan area as a single env_params point, recent date.

One paid call at most (2,900 credits observed; free if rejected). Point 40.7128, -74.0060 (the
docs' env_params example, inside the Quickstart heatmap box), 2026-11-17 14:00 local, filter_type 1,
reference temperature 35 C (the docs example value, as chosen by the user).
Raw files go to results/raw/quickstart/ with unique timestamped names.
"""

from __future__ import annotations

import time

from probe import ROOT, guard_spend, ledger_add, load_env, rest

BODY = {
    "latitude": 40.7128,
    "longitude": -74.0060,
    "temperature": 35,
    "date_time": {"start_date": "2026-11-17", "start_time": "14:00", "filter_type": 1},
}


def main() -> None:
    load_env(ROOT / ".env")
    guard_spend(2900)
    resp = rest("POST", "/env_params", "quickstart/env_params_manhattan_submit", BODY)
    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    if resp.status_code != 200:
        print("rejected:", resp.status_code, body)
        ledger_add(
            "env_params",
            0,
            f"F-29 follow-up Manhattan env_params rejected HTTP {resp.status_code}, free",
        )
        return
    activity_id = body["data"]["activity_id"]
    status = ""
    for i in range(40):
        time.sleep(5)
        poll = rest(
            "GET",
            f"/status/{activity_id}",
            f"quickstart/env_params_manhattan_status_poll{i + 1:02d}",
        )
        data = (poll.json() or {}).get("data") or {}
        status = str(data.get("status", "")).lower()
        if status in {"completed", "failed"}:
            break
    print("final status:", status)


if __name__ == "__main__":
    main()
