"""Registry of every strategy and variant ever evaluated.

Counting ALL trials honestly is what makes the multiple-testing hurdle meaningful: the more ideas
are tried, the higher the bar for any single one. Statuses: evaluated -> candidate -> approved /
rejected (holdout failed; permanent - the same holdout can't be reused to "try again")."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class Registry:
    def __init__(self, path: str = "lab/registry.json"):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else \
            {"trials": 0, "strategies": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, default=str, ensure_ascii=False), encoding="utf-8")

    @property
    def trials(self) -> int:
        return self.data["trials"]

    def record_evaluation(self, report: dict) -> dict:
        name = report["strategy"]
        entry = self.data["strategies"].get(name, {"history": []})
        if entry.get("status") in ("approved", "rejected"):
            raise ValueError(f"{name} is already {entry['status']} on this holdout; create a new strategy "
                             "name for a genuinely different idea.")
        self.data["trials"] += report["param_sets_tested"]
        entry["history"].append({"time": datetime.now().isoformat(timespec="seconds"),
                                 "walk_forward_oos": report["walk_forward_oos"],
                                 "candidate": report["candidate"], "params": report["final_params"]})
        entry.update(status="candidate" if report["candidate"] else "evaluated",
                     description=report["description"], final_params=report["final_params"],
                     last_report=report)
        self.data["strategies"][name] = entry
        self.save()
        return entry

    def record_finalize(self, name: str, result: dict) -> dict:
        entry = self.data["strategies"].get(name)
        if not entry or entry.get("status") != "candidate":
            raise ValueError(f"{name} must be a candidate (all gates passed) before the holdout test")
        entry["holdout"] = result
        entry["status"] = "approved" if result["passed"] else "rejected"
        entry["finalized"] = datetime.now().isoformat(timespec="seconds")
        self.save()
        return entry

    def approved(self) -> dict[str, dict]:
        return {k: v for k, v in self.data["strategies"].items() if v.get("status") == "approved"}

    def summary(self) -> list[dict]:
        rows = []
        for name, e in self.data["strategies"].items():
            wf = (e.get("last_report") or {}).get("walk_forward_oos", {})
            rows.append({"strategy": name, "status": e.get("status"), "wf_n": wf.get("n"),
                         "wf_avg_r": wf.get("avg_r"), "wf_t": wf.get("t"),
                         "holdout_avg_r": (e.get("holdout") or {}).get("holdout", {}).get("avg_r"),
                         "params": e.get("final_params")})
        return rows
