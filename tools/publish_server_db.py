"""Put what the panel has kept from the L2Axis Alt+B Database (server_db.json, next to the panel,
never published as is) into the file that ships with the panel: data/l2axis_db.json.

The panel fills server_db.json by itself while you play: every Database page you open in Alt+B
(an item's drop / spoil list, a monster search, a monster's card) is kept. This script merges it
into data/l2axis_db.json (the newer view of each row wins) with the times cut to the day.

Usage (from the L2AxisControl folder):
    python tools/publish_server_db.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_drops_db as drops  # noqa: E402  (loads the panel module)

DAY = 86400


def to_day(db):
    """Times cut to the start of their day (UTC): enough to tell how old a list is."""
    day = lambda t: int(t) - int(t) % DAY if t else t
    for it in db["items"].values():
        if "seen" in it:
            it["seen"] = day(it["seen"])
        it["pages"] = {p: day(t) for p, t in it.get("pages", {}).items()}
        for r in it.get("rows", {}).values():
            r["seen"] = day(r["seen"])
    for m in db["mobs"].values():
        if "seen" in m:
            m["seen"] = day(m["seen"])
        m.get("stats", {}).pop("spawned", None)  # how many are alive right now: not a fact about the monster
    for e in db["log"]:
        e["at"] = day(e["at"])
    return db


def main():
    panel = drops.load_panel()
    local = panel.ServerDB._read(panel.SERVER_DB_FILE)
    if not local["items"] and not local["mobs"]:
        print(f"Nothing kept yet in {panel.SERVER_DB_FILE.name}: open Alt+B > Database pages in game with the panel running.")
        return 1
    out = panel.ServerDB._read(panel.SERVER_DB_SHIPPED)
    before = (len(out["items"]), sum(len(it.get("rows", {})) for it in out["items"].values()), len(out["mobs"]))
    panel.merge_server_db(out, to_day(local), src_wins_ties=True)
    after = (len(out["items"]), sum(len(it.get("rows", {})) for it in out["items"].values()), len(out["mobs"]))
    doc = {"source": "L2Axis server, Alt+B > Database pages as shown in game",
           "note": "Drop and spoil lists of the L2Axis server, kept by the panel while its players browse them. "
                   "Times are the day the page was seen.",
           **out}
    panel.SERVER_DB_SHIPPED.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{panel.SERVER_DB_SHIPPED.relative_to(panel.APP_DIR)}: items {before[0]} -> {after[0]}, "
          f"rows {before[1]} -> {after[1]}, monsters {before[2]} -> {after[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
