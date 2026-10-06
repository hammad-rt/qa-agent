"""Test-data ledger and admin-restore tracking (RDP: ledger, leftovers, approved cleanup; restore admin changes).

Every record the run creates or changes is listed with a run-ID tag and its final state. Every admin
setting changed must have its original value recorded and a matching verified restore; anything not
restored is reported as a leftover and flagged in the case note.
"""
from __future__ import annotations


class Ledger:
    def __init__(self, state):
        self.state = state
        self.state.data.setdefault("ledger", [])
        self.state.data.setdefault("settings", {})

    @property
    def run_tag(self) -> str:
        return f"qa-{self.state.dir.name}"

    def add(self, case_id: str, app: str, record: str, action: str, final_state: str = "", note: str = "") -> dict:
        e = {"case_id": case_id, "app": app, "record": record, "action": action, "final_state": final_state,
             "note": note, "tag": self.run_tag, "cleaned": False}
        for old in self.state.data["ledger"]:
            if old["record"] == record and old["app"] == app:
                old.update({k: v for k, v in e.items() if v})
                self.state.save()
                return old
        self.state.data["ledger"].append(e)
        self.state.save()
        return e

    def record_original(self, case_id: str, app: str, setting: str, value: str) -> dict:
        key = f"{app}:{setting}"
        s = self.state.data["settings"].setdefault(key, {"case_id": case_id, "app": app, "setting": setting,
                                                          "original": value, "restored": None, "verified": False})
        self.state.save()
        return s

    def record_restored(self, app: str, setting: str, value: str, verified: bool) -> dict:
        key = f"{app}:{setting}"
        s = self.state.data["settings"].get(key)
        if s is None:
            raise KeyError(f"no original value recorded for {key}")
        s["restored"], s["verified"] = value, bool(verified and str(value).strip() == str(s["original"]).strip())
        self.state.save()
        return s

    def unrestored(self, case_id: str | None = None) -> list[dict]:
        return [s for s in self.state.data["settings"].values()
                if not s["verified"] and (case_id is None or s["case_id"] == case_id)]

    def leftovers(self) -> list[dict]:
        return [e for e in self.state.data["ledger"] if not e["cleaned"] and e["action"] in ("created", "changed")]

    def mark_cleaned(self, record: str, app: str) -> None:
        for e in self.state.data["ledger"]:
            if e["record"] == record and e["app"] == app:
                e["cleaned"] = True
        self.state.save()
