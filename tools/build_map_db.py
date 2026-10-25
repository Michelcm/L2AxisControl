"""Builds data/map_db.json from the L2J datapack (GPL-3.0): the Gatekeeper teleports (from -> to,
coordinates, price) used to suggest the fastest way to a boss.

Usage: python tools/build_map_db.py <path to l2j datapack checkout>
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "AppData/Local/Temp/l2jdp"
SQL = ROOT / "src/main/resources/sql/teleport.sql"
OUT = Path(__file__).resolve().parent.parent / "data" / "map_db.json"

ROW = re.compile(r"\('((?:[^'\\]|\\.|'')*)',(\d+),(-?\d+),(-?\d+),(-?\d+),(\d+),(\d),(\d+)\)")

# the same town is written in several ways in the descriptions
TOWN_ALIASES = {
    "town of gludio": "Gludio", "gludio": "Gludio", "gludio castle town": "Gludio",
    "gludin": "Gludin", "gludin village": "Gludin", "town of gludin": "Gludin",
    "dion": "Dion", "town of dion": "Dion", "dion castle town": "Dion",
    "giran": "Giran", "town of giran": "Giran", "giran castle town": "Giran", "giran town": "Giran",
    "oren": "Oren", "town of oren": "Oren", "oren castle town": "Oren",
    "aden": "Aden", "town of aden": "Aden", "aden castle town": "Aden",
    "rune": "Rune", "rune township": "Rune", "town of rune": "Rune", "rune castle town": "Rune",
    "goddard": "Goddard", "town of goddard": "Goddard", "goddard castle town": "Goddard",
    "schuttgart": "Schuttgart", "town of schuttgart": "Schuttgart",
    "heine": "Heine", "town of heine": "Heine",
    "hunters village": "Hunters Village", "hunter's village": "Hunters Village", "hunters' village": "Hunters Village",
    "ti village": "Talking Island", "talking island village": "Talking Island", "talking island": "Talking Island",
    "elven village": "Elven Village", "de village": "Dark Elf Village", "dark elven village": "Dark Elf Village",
    "dark elf village": "Dark Elf Village", "orc village": "Orc Village", "dwarven village": "Dwarven Village",
    "floran village": "Floran", "floran": "Floran", "kamael village": "Kamael Village",
    "town of floran": "Floran", "kamael vilage": "Kamael Village", "ti": "Talking Island", "hunters": "Hunters Village", "giran harbor": "Giran Harbor", "rune harbor": "Rune Harbor",
}


def town(name):
    key = re.sub(r"\s+", " ", name.strip().lower())
    return TOWN_ALIASES.get(key)


def main():
    text = SQL.read_text(encoding="utf-8")
    text = text.split("INSERT INTO `teleport` VALUES", 1)[1]
    teleports, towns = [], {}
    for desc, tid, x, y, z, price, noble, item in ROW.findall(text):
        desc = desc.replace("''", "'").replace("\\'", "'")
        if "->" not in desc:
            continue
        src, dst = [s.strip() for s in desc.split("->", 1)]
        src_town = town(src)
        if not src_town or noble == "1" or item != "57":
            continue  # only normal Gatekeeper teleports paid in adena, from a known town
        dst_town = town(dst)
        teleports.append({"from": src_town, "to": dst, "toTown": dst_town, "x": int(x), "y": int(y),
                          "price": int(price)})
        if dst_town:
            towns.setdefault(dst_town, (int(x), int(y)))
    # de-duplicate (same origin and destination)
    seen, out = set(), []
    for t in teleports:
        k = (t["from"], t["to"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    data = {
        "source": "L2J Server datapack (GPL-3.0), sql/teleport.sql",
        "towns": {k: {"x": v[0], "y": v[1]} for k, v in sorted(towns.items())},
        "teleports": out,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(OUT, len(out), "teleports,", len(towns), "towns:", ", ".join(sorted(towns)))


if __name__ == "__main__":
    main()
