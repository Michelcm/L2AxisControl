# data/drops_db.json, data/items_db.json, data/map_db.json and data/spoil_db.json

Drop and spoil chances, NPC names and levels, spawn locations and NPC exchange lists in
`drops_db.json`, and the item passive bonuses and masterwork (Foundation) chains in `items_db.json`, are derived from the **L2J Server datapack** (High Five, release 2.6.2.0):

- https://bitbucket.org/l2jserver/l2j-server-datapack
- License: **GNU General Public License v3.0** (https://www.gnu.org/licenses/gpl-3.0.html)

The Gatekeeper teleports in `map_db.json` (from `sql/teleport.sql`) and the map regions used by the panel come from the same datapack.

These files are therefore distributed under the GPL-3.0, separately from the panel's own code (MIT).
Area names come from the nearest hunting zone in the player's game client. Spawns inside the Seven Signs
catacombs and necropolises are recognised from one point inside each of them (`sql/random_spawn_loc.sql`
in the same datapack) and shown at the dungeon's entrance.

Interior maps of dungeons are **not** included: the "Interior map" button opens them in the browser
from the L2J.RU database (https://l2j.ru/highfive/index.php?p=10), whose authors drew them.

These are the official High Five values. L2Axis may use its own rates and drop lists, so the panel
shows them as a guide, not a guarantee.

Regenerate with:

    python tools/build_drops_db.py <path to l2j-server-datapack checkout>
    python tools/build_items_db.py <path to l2j-server-datapack checkout>
    python tools/build_map_db.py <path to l2j-server-datapack checkout>
    python tools/build_spoil_db.py <path to l2j-server-datapack checkout>

Item icons are **not** included in this repository: the panel reads them from the player's own
game client (`SysTextures`) and caches them locally in `icons/`.
