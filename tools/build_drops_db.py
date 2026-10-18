"""Build data/drops_db.json: where each crafting-related item drops (official High Five data).

Sources
- Drop/spoil lists and NPC names: L2J Server datapack (High Five), data/stats/npcs/*.xml (GPL-3.0)
  https://bitbucket.org/l2jserver/l2j-server-datapack
- NPC locations: the same datapack's spawn lists (sql/spawnlist.sql, raidboss_spawnlist.sql,
  data/spawnlist/*.xml, data/hellbound/hellboundSpawns.xml);
  NPCs spawned only by scripts (instances, events) have no area.
  Raid/grand bosses use the client's own raiddata-e.dat zone instead (grandboss_data.sql holds placeholders)
- NPC exchanges (unseal, upgrades, token shops): the same datapack's data/multisell/*.xml
- Area names: the game client's HuntingZone-e.dat (nearest hunting zone to the spawn)

The chronicle no longer changes, so this runs once and the JSON ships with the panel.
L2Axis may use its own rates and drops: the panel labels this data as "official H5".

Usage:
    python tools/build_drops_db.py <path to l2j-server-datapack checkout>
(run from the L2AxisControl folder, inside the game folder, so the client files can be read)
"""
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MAX_SOURCES = 12  # per item and kind (drop / spoil), best chances first


def load_panel():
    spec = importlib.util.spec_from_file_location("panel", HERE / "l2axis_control.pyw")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wanted_items(panel):
    """Every item used by recipes: the recipes themselves, products and materials."""
    db = panel.build_recipe_db()
    ids = set()
    for r in db["recipes"]:
        ids.add(r["recipeItem"])
        ids.add(r["product"])
        ids.update(m for m, _ in r["mats"])
    return ids


def hunting_zones(panel):
    r = panel._DatReader(panel.l2_decrypt413(panel.GAME_DIR / "HuntingZone-e.dat"))
    zones = []
    for _ in range(r.u32()):
        _zid, _typ, lvl, _unk = r.u32(), r.u32(), r.u32(), r.u32()
        x, y, _z = r.f32(), r.f32(), r.f32()
        r.ascf()
        r.u32()
        name = r.ascf()
        if x or y:
            zones.append((x, y, name, lvl))
    return zones


def spawn_points(datapack):
    """npc id -> [(x, y)] from the regular and raid boss spawn lists."""
    points = defaultdict(list)
    sql = datapack / "src/main/resources/sql"
    row = re.compile(r"\(([^()]*)\)")
    for line in (sql / "spawnlist.sql").read_text(encoding="utf-8", errors="replace").splitlines():
        for m in row.finditer(line):
            parts = [p.strip().strip("'\"") for p in m.group(1).split(",")]
            # (location, count, npc_templateid, locx, locy, locz, randomx, randomy, heading, respawn_delay, ...)
            if len(parts) >= 6 and parts[2].isdigit():
                try:
                    points[int(parts[2])].append((int(parts[3]), int(parts[4])))
                except ValueError:
                    pass
    for line in (sql / "raidboss_spawnlist.sql").read_text(encoding="utf-8", errors="replace").splitlines():
        for m in row.finditer(line):
            parts = [p.strip().strip("'\"") for p in m.group(1).split(",")]
            # (boss_id, amount, loc_x, loc_y, loc_z, ...)
            if len(parts) >= 5 and parts[0].isdigit():
                try:
                    points[int(parts[0])].append((int(parts[2]), int(parts[3])))
                except ValueError:
                    pass
    data = datapack / "src/main/resources/data"
    for xml_file in list((data / "spawnlist").glob("*.xml")) + [data / "hellbound/hellboundSpawns.xml"]:
        for npc in ET.parse(xml_file).getroot().iter("npc"):
            loc = npc.find("location")
            src = loc if loc is not None else npc
            if src.get("x") and src.get("y"):
                points[int(npc.get("id"))].append((int(src.get("x")), int(src.get("y"))))
    return points


def _commented(xml_file):
    """Parse keeping comments: multisells name every id in the comment just before it."""
    return ET.parse(xml_file, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))).getroot()


def multisells(datapack, wanted, names):
    """item id -> {"buy": [...], "into": [...]} from NPC exchange lists (Mammon unseal, Ishuma,
    Olympiad, Kirklan...). "buy" = what it costs to get the item; "into" = what the item becomes."""
    shops = defaultdict(lambda: {"buy": [], "into": []})
    for xml_file in sorted((datapack / "src/main/resources/data/multisell").glob("*.xml")):
        root = _commented(xml_file)
        npc_names = []
        npcs = root.find("npcs")
        if npcs is not None:
            last = None
            for node in npcs:
                if node.tag == "npc":
                    last = node
                elif last is not None and node.tag is ET.Comment:
                    npc_names.append(re.sub(r"\s*\(.*\)$", "", node.text.strip()))
                    last = None
        if not npc_names:
            continue  # lists opened only by scripts or the community board
        for entry in root.findall("item"):
            comment, ingredients, products = None, [], []
            for node in entry:
                if node.tag is ET.Comment:
                    comment = node.text.strip()
                    continue
                item_id, count = int(node.get("id")), int(node.get("count"))
                if comment:
                    names.setdefault(item_id, comment)
                (ingredients if node.tag == "ingredient" else products).append([item_id, count])
                comment = None
            if len(products) != 1 or "{PvP}" in names.get(products[0][0], ""):
                continue
            product = products[0][0]
            pname = names.get(product, "")
            if len(ingredients) == 1 and (names.get(ingredients[0][0], "").startswith(pname + " - ")
                                          or names.get(ingredients[0][0], "").startswith(pname)):
                continue  # Special Ability removal, {PvP} reversal or same-name swap: not a way to get it
            deal = {"npc": sorted(set(npc_names))[:3], "cost": ingredients, "product": product}
            if product in wanted and len(shops[product]["buy"]) < 4:
                shops[product]["buy"].append(deal)
            for item_id, count in ingredients:
                if item_id in wanted and count == 1 and len(shops[item_id]["into"]) < 4:
                    shops[item_id]["into"].append(deal)
    return shops


def main(datapack):
    datapack = Path(datapack)
    panel = load_panel()
    wanted = wanted_items(panel)
    stats = panel.build_stats_db() or {"items": {}}
    wanted |= {int(k) for k in stats["items"]}  # gear shown in the Compare tab
    zones = hunting_zones(panel)
    spawns = spawn_points(datapack)

    bosses = panel.build_boss_db()

    def area_of(npc_id):
        boss = bosses.get(str(npc_id))
        if boss and boss.get("zone"):
            return boss["zone"].strip()
        pts = spawns.get(npc_id)
        if not pts or not zones:
            return None
        names = Counter(min(zones, key=lambda z: (z[0] - x) ** 2 + (z[1] - y) ** 2)[2] for x, y in pts)
        return names.most_common(1)[0][0].strip()

    npcs, sources = {}, defaultdict(lambda: {"drop": [], "spoil": []})
    for xml_file in sorted((datapack / "src/main/resources/data/stats/npcs").glob("*.xml")):
        root = ET.parse(xml_file).getroot()
        for npc in root.iter("npc"):
            npc_id = int(npc.get("id"))
            lists = npc.find("dropLists")
            if lists is None:
                continue
            info = {"name": npc.get("name"), "level": int(npc.get("level", 0)), "type": npc.get("type")}
            used = False
            death = lists.find("death")
            if death is not None:
                for group in death.findall("group"):
                    gchance = float(group.get("chance", 100))
                    for item in group.findall("item"):
                        item_id = int(item.get("id"))
                        if item_id in wanted:
                            chance = gchance * float(item.get("chance", 100)) / 100
                            sources[item_id]["drop"].append([npc_id, round(chance, 4),
                                                             int(item.get("min", 1)), int(item.get("max", 1))])
                            used = True
                for item in death.findall("item"):  # ungrouped items
                    item_id = int(item.get("id"))
                    if item_id in wanted:
                        sources[item_id]["drop"].append([npc_id, round(float(item.get("chance", 100)), 4),
                                                         int(item.get("min", 1)), int(item.get("max", 1))])
                        used = True
            corpse = lists.find("corpse")
            if corpse is not None:
                for item in corpse.findall("item"):
                    item_id = int(item.get("id"))
                    if item_id in wanted:
                        sources[item_id]["spoil"].append([npc_id, round(float(item.get("chance", 100)), 4),
                                                          int(item.get("min", 1)), int(item.get("max", 1))])
                        used = True
            if used:
                npcs[npc_id] = info

    for kinds in sources.values():
        for kind in ("drop", "spoil"):
            # same-named NPC variants (instance copies) count once: the best chance wins,
            # preferring the variant with a known spawn so the player can find it
            best = {}
            for s in sorted(kinds[kind], key=lambda s: (-s[1], s[0] not in spawns)):
                best.setdefault(npcs[s[0]]["name"], s)
            kinds[kind] = sorted(best.values(), key=lambda s: -s[1])[:MAX_SOURCES]
    used_npcs = {s[0] for kinds in sources.values() for k in kinds.values() for s in k}
    for npc_id in used_npcs:
        npcs[npc_id]["area"] = area_of(npc_id)

    names = {}
    shops = multisells(datapack, wanted, names)
    shop_ids = {i for s in shops.values() for k in s.values() for d in k for i, _ in d["cost"] + [[d["product"], 1]]}
    out = {
        "source": "L2J Server datapack (High Five) - https://bitbucket.org/l2jserver/l2j-server-datapack - GPL-3.0",
        "note": "Official High Five drop/spoil chances. L2Axis may use its own rates and drop lists.",
        "npcs": {str(k): v for k, v in sorted(npcs.items()) if k in used_npcs},
        "items": {str(k): v for k, v in sorted(sources.items())},
        "shops": {str(k): v for k, v in sorted(shops.items())},
        # names for exchange ingredients the client name table may not cover (Fame = -300, etc.)
        "names": {str(k): v for k, v in sorted(names.items()) if k in shop_ids},
    }
    target = HERE / "data" / "drops_db.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    with_src = sum(1 for v in sources.values() if v["drop"] or v["spoil"])
    print(f"{len(wanted)} crafting items, {with_src} with sources, {len(used_npcs)} NPCs -> {target} "
          f"({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
