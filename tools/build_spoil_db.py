"""Build data/spoil_db.json: every monster that can be spoiled, what it gives and where it spawns
(official High Five data), for the Spoil tab.

Sources (L2J Server datapack, High Five, GPL-3.0 — https://bitbucket.org/l2jserver/l2j-server-datapack)
- data/stats/npcs/*.xml: monsters, levels and their "corpse" (spoil) lists
- data/stats/items/*.xml: item names and kinds (material, recipe...)
- spawn lists (sql/spawnlist.sql, data/spawnlist/*.xml...): where each monster spawns, grouped in spots
Area names come from the game client's HuntingZone-e.dat (nearest hunting zone).

Usage (from the L2AxisControl folder, inside the game folder):
    python tools/build_spoil_db.py <path to l2j-server-datapack checkout>
"""
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_drops_db as drops  # noqa: E402  (spawn lists, hunting zones, panel loader)

HERE = Path(__file__).resolve().parent.parent
SPOT = 3000        # spawn points closer than this (game units) form one spot
MAX_SPOTS = 6      # per monster, the biggest spots


def item_info(datapack):
    names, kinds = {}, {}
    for xml_file in (datapack / "src/main/resources/data/stats/items").glob("*.xml"):
        for it in ET.parse(xml_file).getroot().iter("item"):
            iid = int(it.get("id"))
            names[iid] = it.get("name")
            kind = next((s.get("val") for s in it.findall("set") if s.get("name") == "etcitem_type"), None)
            kinds[iid] = (kind or it.get("type") or "").lower()
    return names, kinds


def spawn_points_z(datapack):
    """npc id -> [(x, y, z)]: like build_drops_db.spawn_points, keeping the height (tower floors)."""
    import re
    points = defaultdict(list)
    sql = datapack / "src/main/resources/sql"
    row = re.compile(r"\(([^()]*)\)")
    for line in (sql / "spawnlist.sql").read_text(encoding="utf-8", errors="replace").splitlines():
        for m in row.finditer(line):
            parts = [p.strip().strip("'\"") for p in m.group(1).split(",")]
            if len(parts) >= 6 and parts[2].isdigit():
                try:
                    points[int(parts[2])].append((int(parts[3]), int(parts[4]), int(parts[5])))
                except ValueError:
                    pass
    data = datapack / "src/main/resources/data"
    for xml_file in list((data / "spawnlist").glob("*.xml")) + [data / "hellbound/hellboundSpawns.xml"]:
        for npc in ET.parse(xml_file).getroot().iter("npc"):
            loc = npc.find("location")
            src = loc if loc is not None else npc
            if src.get("x") and src.get("y"):
                points[int(npc.get("id"))].append((int(src.get("x")), int(src.get("y")), int(src.get("z") or 0)))
    return points


def spots(points):
    groups = defaultdict(list)
    for x, y, z in points:
        # cells by height too: tower floors stay apart
        groups[(x // SPOT, y // SPOT, z // 600)].append((x, y, z))
    # merge neighbouring cells into the bigger one
    out = []
    for cell, pts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        cz = sum(p[2] for p in pts) / len(pts)
        near = next((o for o in out if abs(o[0] - cx) < SPOT * 1.5 and abs(o[1] - cy) < SPOT * 1.5
                     and abs(o[2] - cz) < 600), None)
        if near:
            n = near[3] + len(pts)
            near[0] = (near[0] * near[3] + cx * len(pts)) / n
            near[1] = (near[1] * near[3] + cy * len(pts)) / n
            near[2] = (near[2] * near[3] + cz * len(pts)) / n
            near[3] = n
        else:
            out.append([cx, cy, cz, len(pts)])
    out.sort(key=lambda o: -o[3])
    return [[round(x), round(y), round(z), n] for x, y, z, n in out[:MAX_SPOTS]]


def main(datapack):
    datapack = Path(datapack)
    panel = drops.load_panel()
    zones = drops.hunting_zones(panel)
    spawn = spawn_points_z(datapack)
    names, kinds = item_info(datapack)

    def area(x, y):
        return min(zones, key=lambda z: (z[0] - x) ** 2 + (z[1] - y) ** 2)[2].strip() if zones else None

    mobs, items = {}, defaultdict(list)
    for xml_file in sorted((datapack / "src/main/resources/data/stats/npcs").glob("*.xml")):
        for npc in ET.parse(xml_file).getroot().iter("npc"):
            if npc.get("type") not in ("L2Monster", "L2RaidBoss", "L2FlyMonster"):
                continue
            lists = npc.find("dropLists")
            corpse = lists.find("corpse") if lists is not None else None
            if corpse is None:
                continue
            npc_id = int(npc.get("id"))
            pts = spawn.get(npc_id)
            if not pts:
                continue  # spawned only by scripts (instances, quests): nowhere to point to
            spoil = []
            for it in corpse.findall("item"):
                iid = int(it.get("id"))
                spoil.append([iid, round(float(it.get("chance", 100)), 4), int(it.get("min", 1)), int(it.get("max", 1))])
            if not spoil:
                continue
            sp = spots(pts)
            mobs[npc_id] = {"name": npc.get("name"), "level": int(npc.get("level", 0)),
                            "area": area(sp[0][0], sp[0][1]), "spots": sp, "spoil": spoil}
            for iid, chance, lo, hi in spoil:
                items[iid].append(npc_id)

    # same-named variants: keep the one with most spawn points
    best = {}
    for npc_id, m in mobs.items():
        key = (m["name"], m["level"])
        if key not in best or sum(s[2] for s in m["spots"]) > sum(s[2] for s in mobs[best[key]]["spots"]):
            best[key] = npc_id
    keep = set(best.values())
    mobs = {k: v for k, v in mobs.items() if k in keep}
    used_items = {iid for m in mobs.values() for iid, *_ in m["spoil"]}
    out = {
        "source": "L2J Server datapack (High Five) - GPL-3.0; areas from the game client",
        "note": "Official High Five spoil chances. L2Axis may use its own rates and lists.",
        "mobs": {str(k): v for k, v in sorted(mobs.items())},
        "items": {str(i): {"name": names.get(i, f"#{i}"), "kind": kinds.get(i, "")} for i in sorted(used_items)},
    }
    target = HERE / "data" / "spoil_db.json"
    target.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(mobs)} monsters, {len(used_items)} items -> {target} ({target.stat().st_size // 1024} KB)")
    print(Counter(out["items"][str(i)]["kind"] for i in used_items).most_common(12))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
