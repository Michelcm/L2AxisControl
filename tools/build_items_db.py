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
# stats that change what a player sees in the status window or deals as damage
STAT_OPS = ("add", "sub", "mul", "basemul")
POOL_EFFECTS = {"MaxHp": "maxHp", "MaxMp": "maxMp", "MaxCp": "maxCp"}


def load_items(data):
    items = {}
    for xml_file in (data / "stats/items").glob("*.xml"):
        for it in ET.parse(xml_file).getroot().iter("item"):
            sets = {s.get("name"): s.get("val") for s in it.findall("set")}
            items[int(it.get("id"))] = {"name": it.get("name"), "type": it.get("type"),
                                        "skill": sets.get("item_skill")}
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
    unseal = {}  # sealed item -> unsealed item (Blacksmith of Mammon)
    for xml_file in (data / "multisell").glob("31126*.xml"):
        for entry in ET.parse(xml_file).getroot().findall("item"):
            ingredients = entry.findall("ingredient")
            product = int(entry.find("production").get("id"))
            first = int(ingredients[0].get("id"))
            if items.get(first, {}).get("name", "").startswith("Sealed ") and \
                    items[first]["name"][7:] == items.get(product, {}).get("name"):
                unseal.setdefault(first, product)

    masterwork = {}
    for foundation, info in rare.items():
        if foundation not in varnish:
            continue
        result = varnish[foundation]["result"]
        final = unseal.get(result, result)
        chain = {"product": info["product"], "rarity": info["rarity"], "foundation": foundation,
                 "varnish": varnish[foundation]["cost"], "result": result, "final": final}
        masterwork[str(result)] = chain
        if final != result:
            masterwork[str(final)] = chain
    out = {
        "source": "L2J Server datapack (High Five) - https://bitbucket.org/l2jserver/l2j-server-datapack - GPL-3.0",
        "note": "Passive item bonuses and masterwork chains, official High Five values. L2Axis may differ.",
        "bonus": bonus,
        "masterwork": masterwork,
        "names": {str(k): items[k]["name"] for k in sorted({c["foundation"] for c in masterwork.values()}
                                                          | {i for c in masterwork.values() for i, _ in c["varnish"]})},
    }
    target = HERE / "data" / "items_db.json"
    target.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(bonus)} items with passive bonuses, {len(masterwork)} masterwork items -> {target} "
          f"({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
