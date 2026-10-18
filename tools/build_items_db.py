"""Build data/items_db.json: item passive bonuses and the masterwork (Foundation) chains.

Base stats (P.Atk, P.Def...) come from the game client at runtime. What the client does not have is
what an item's passive skill adds on top: a masterwork "Dynasty Blade - Great Gale" has the same
base stats as a plain Dynasty Blade plus +5% Atk. Spd.; a masterwork Dynasty Gaiters adds Max HP
and P. Def. That data, and how a masterwork is obtained, comes from the L2J Server datapack
(High Five, GPL-3.0): data/stats/items, data/stats/skills, data/recipes.xml, data/multisell.

Usage:
    python tools/build_items_db.py <path to l2j-server-datapack checkout>
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MAMMON_FOUNDATION_LIST = "311262513.xml"
# Blacksmith of Mammon (Ancient Adena) and Ishuma (Adena): two weapons + Dualsword Craft Stamp -> dual
DUAL_LISTS = {"311262503.xml": "Blacksmith of Mammon", "326150001.xml": "Ishuma"}
TOP_FAMILIES = re.compile(r"^(Dynasty|Icarus|Vesper)\b")
# stats that change what a player sees in the status window or deals as damage
STAT_OPS = ("add", "sub", "mul", "basemul")
POOL_EFFECTS = {"MaxHp": "maxHp", "MaxMp": "maxMp", "MaxCp": "maxCp"}


def plural_free(name):
    return re.sub(r"s\b", "", name.lower())


# accessory slots (client body parts) shown in the Craft tab
ACCESSORY_SLOTS = {"underwear": "shirt", "waist": "belt", "rbracelet": "bracelet", "deco1": "talisman",
                   "back": "cloak", "hair": "hair", "hairall": "hair"}
ACCESSORY_SKIP = re.compile(r"limited period|[Ee]vent|\{PvP\}|Shadow Item|Test|Agathion|Mount|Poison Bottle|"
                            r"Undergarment|Swimsuit|Bikini|Underwear of|Pailaka|_|Summon")


def load_items(data):
    items = {}
    for xml_file in (data / "stats/items").glob("*.xml"):
        for it in ET.parse(xml_file).getroot().iter("item"):
            sets = {s.get("name"): s.get("val") for s in it.findall("set")}
            stats = []
            for block in it.findall("for"):  # the item's own stats (enchant rows excluded)
                for node in block:
                    if node.tag in ("add", "mul") and node.get("stat"):
                        try:
                            stats.append([node.tag, node.get("stat"), float(node.get("val"))])
                        except (TypeError, ValueError):
                            pass
            items[int(it.get("id"))] = {"name": it.get("name"), "type": it.get("type"),
                                        "skill": sets.get("item_skill"), "bodypart": sets.get("bodypart"),
                                        "grade": sets.get("crystal_type") or "NONE", "stats": stats}
    return items


def load_skills(data):
    skills = {}
    for xml_file in (data / "stats/skills").glob("*.xml"):
        for sk in ET.parse(xml_file).getroot().iter("skill"):
            skills[int(sk.get("id"))] = sk
    return skills


def skill_effects(skill, level):
    """[[op, stat, value], ...] of a passive skill at a level (tables like #pAtk resolved)."""
    tables = {}
    for table in skill.iter("table"):
        tables[table.get("name")] = table.text.split()

    def value(raw):
        raw = (raw or "").strip()
        if raw.startswith("#"):
            values = tables.get(raw, [])
            raw = values[min(level, len(values)) - 1] if values else "0"
        try:
            return float(raw)
        except ValueError:
            return None

    out = []
    for effect in skill.iter("effect"):
        if effect.get("name") == "TalismanSlot":
            param = effect.find("param")
            if param is not None and param.get("slots"):
                out.append(["add", "talismanSlots", float(param.get("slots"))])
            continue
        pool = POOL_EFFECTS.get(effect.get("name"))
        if pool:
            param = effect.find("param")
            if param is not None and value(param.get("power")):
                out.append(["add", pool, value(param.get("power"))])
    for node in skill.iter():
        if node.tag in STAT_OPS and node.get("stat"):
            v = value(node.get("val"))
            if v is not None:
                out.append([node.tag, node.get("stat"), round(v, 4)])
    return out


def main(datapack):
    data = Path(datapack) / "src/main/resources/data"
    items, skills = load_items(data), load_skills(data)

    # passive bonuses of every equippable item that has an item skill
    bonus = {}
    for item_id, it in items.items():
        if not it["skill"] or it["type"] not in ("Weapon", "Armor"):
            continue
        effects, names = [], []
        for part in it["skill"].split(";"):
            m = re.fullmatch(r"\s*(\d+)-(\d+)\s*", part)
            sk = skills.get(int(m.group(1))) if m else None
            if sk is None or sk.find(".//set[@name='operateType'][@val='P']") is None:
                continue  # only passive skills change the item's stats
            effects += skill_effects(sk, int(m.group(2)))
            names.append(sk.get("name"))
        if effects:
            bonus[str(item_id)] = {"skill": " / ".join(names), "effects": effects}

    # crafted product -> rare (Foundation) result -> Mammon varnish -> masterwork (-> unseal for armor)
    rare = {}
    for rec in ET.parse(data / "recipes.xml").getroot().iter("item"):
        prod, rare_prod = rec.find("production"), rec.find("productionRare")
        if rare_prod is not None:
            rare[int(rare_prod.get("id"))] = {"product": int(prod.get("id")),
                                              "rarity": float(rare_prod.get("rarity"))}
    varnish = {}
    for entry in ET.parse(data / "multisell" / MAMMON_FOUNDATION_LIST).getroot().findall("item"):
        ingredients = [(int(i.get("id")), int(i.get("count"))) for i in entry.findall("ingredient")]
        product = int(entry.find("production").get("id"))
        for item_id, _ in ingredients:
            if item_id in rare:
                varnish[item_id] = {"result": product, "cost": [list(i) for i in ingredients if i[0] != item_id]}
    unseal, unseal_cost = {}, {}  # sealed item -> unsealed item (Blacksmith of Mammon), and its price
    for xml_file in (data / "multisell").glob("31126*.xml"):
        for entry in ET.parse(xml_file).getroot().findall("item"):
            ingredients = entry.findall("ingredient")
            product = int(entry.find("production").get("id"))
            first = int(ingredients[0].get("id"))
            # the client is inconsistent with plurals: "Sealed Dynasty Earring" -> "Dynasty Earrings"
            if items.get(first, {}).get("name", "").startswith("Sealed ") and \
                    plural_free(items[first]["name"][7:]) == plural_free(items.get(product, {}).get("name", "")):
                if first not in unseal:
                    unseal[first] = product
                    unseal_cost[first] = [[int(i.get("id")), int(i.get("count"))] for i in ingredients[1:]]

    masterwork = {}
    for foundation, info in rare.items():
        if foundation not in varnish:
            continue
        result = varnish[foundation]["result"]
        final = unseal.get(result, result)
        chain = {"product": info["product"], "rarity": info["rarity"], "foundation": foundation,
                 "varnish": varnish[foundation]["cost"], "result": result, "final": final,
                 "unseal": unseal_cost.get(result, [])}
        masterwork[str(result)] = chain
        if final != result:
            masterwork[str(final)] = chain
    # dual swords / dual daggers have no recipe: the NPC combines two weapons
    combos = {}
    for list_file, npc in DUAL_LISTS.items():
        for entry in ET.parse(data / "multisell" / list_file).getroot().findall("item"):
            product = int(entry.find("production").get("id"))
            if not TOP_FAMILIES.match(items.get(product, {}).get("name", "")):
                continue
            mats = [[int(i.get("id")), int(i.get("count"))] for i in entry.findall("ingredient")]
            combo = combos.setdefault(str(product), {"product": product, "mats": mats, "npc": []})
            combo["npc"].append({"npc": npc, "cost": mats[-1]})
            # a dual made from a masterwork weapon is the dual's masterwork
            for item_id, _ in mats:
                chain = masterwork.get(str(item_id))
                if chain and str(product) not in masterwork:
                    base = next((c["product"] for c in combos.values() if c["product"] != product and
                                 items[c["product"]]["name"] == items[product]["name"].split(" - ")[0]), None)
                    masterwork[str(product)] = {**chain, "result": product, "final": product, "dual": True,
                                                "product": base or chain["product"]}
    # second pass: masterwork duals whose plain dual was read after them
    for key, chain in masterwork.items():
        if chain.get("dual"):
            plain = items[int(key)]["name"].split(" - ")[0]
            chain["product"] = next((c["product"] for c in combos.values()
                                     if items[c["product"]]["name"] == plain), chain["product"])
    combo_ids = {i for c in combos.values() for i, _ in c["mats"]}

    # accessories: no recipe, but they help (shirts, belts, bracelets, talismans, cloaks, hair)
    accessories = {}
    for item_id, it in items.items():
        slot = ACCESSORY_SLOTS.get(it["bodypart"])
        if not slot or ACCESSORY_SKIP.search(it["name"] or ""):
            continue
        effects, actives = list(it["stats"]), []
        for part in (it["skill"] or "").split(";"):
            m = re.fullmatch(r"\s*(\d+)-(\d+)\s*", part)
            sk = skills.get(int(m.group(1))) if m else None
            if sk is None:
                continue
            if sk.find(".//set[@name='operateType'][@val='P']") is not None:
                effects += skill_effects(sk, int(m.group(2)))
            else:
                sets = {s.get("name"): s.get("val") for s in sk.findall("set")}
                active = [e for e in skill_effects(sk, int(m.group(2))) if e[2] != (1 if e[0] == "mul" else 0)]
                actives.append({"name": sk.get("name"), "effects": active,
                                "time": int(sets.get("abnormalTime") or 0) if (sets.get("abnormalTime") or "").isdigit() else 0,
                                "reuse": int(sets.get("reuseDelay") or 0) // 1000
                                if (sets.get("reuseDelay") or "").isdigit() else 0})
        effects = [e for e in effects if e[2] != (1 if e[0] == "mul" else 0)]
        if slot == "hair" and not effects and not any(a["effects"] for a in actives):
            continue  # cosmetic hats
        accessories[str(item_id)] = {"name": it["name"], "slot": slot, "grade": it["grade"],
                                     "effects": effects, "actives": actives}
    out = {
        "source": "L2J Server datapack (High Five) - https://bitbucket.org/l2jserver/l2j-server-datapack - GPL-3.0",
        "note": "Passive item bonuses and masterwork chains, official High Five values. L2Axis may differ.",
        "bonus": bonus,
        "masterwork": masterwork,
        "combos": {k: v for k, v in combos.items() if " - " not in items[v["product"]]["name"]},
        "accessories": accessories,
        "names": {str(k): items[k]["name"] for k in sorted({c["foundation"] for c in masterwork.values()}
                                                          | {i for c in masterwork.values() for i, _ in c["varnish"]}
                                                          | combo_ids | {int(k) for k in combos}
                                                          | {i for c in masterwork.values() for i, _ in c.get("unseal", [])}
                                                          | {c["result"] for c in masterwork.values()}
                                                          | {c["final"] for c in masterwork.values()})},
    }
    target = HERE / "data" / "items_db.json"
    target.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(accessories)} accessories, "
          f"{len(bonus)} items with passive bonuses, {len(masterwork)} masterwork items -> {target} "
          f"({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
