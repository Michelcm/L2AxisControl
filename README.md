# L2Axis Control

A floating control panel for players of the **L2Axis** (Lineage II High Five) server, styled after the game's own windows.
It puts in one place what you usually keep in your head or in a notepad: your game windows, ping, boss respawns,
server events, votes, timers and a crafting guide.

**New here and not a programmer?** Read the step-by-step guide:
**[English guide](docs/GUIDE.md)** · **[Guia em português](docs/GUIA.md)**

> 🇧🇷 **Resumo em português** no fim da página.

![Clients tab](docs/img/clients.png)

## Features

| Tab | What it does |
|---|---|
| **Clients** | Account cards (up to 10): open, focus, minimize and close each game window. Each card shows whether the character is in game, the region, time online and HP/MP. A **monitor map** draws your monitors as they are: click one to arrange the windows on it, or drag an account onto a monitor to move its window there. Save and restore the layout. Optional **auto-login** (see below). |
| **Network** | Live ping and packet loss to the game server with a 2-minute graph. Cloudflare WARP status and on/off toggle. |
| **Bosses** | Respawn countdowns with date and time, filters, search, sort and level ranges, level/area/coordinates from the game's own data files. A **map** in a side pane attached to the panel (the game's own world map, read from your client) shows every boss, area names, the server's Alt+B teleports, the area each of your accounts is in, the Tower of Insolence floor of each boss, and the fastest way there: the nearest Alt+B teleport (or Gatekeeper), then the run. |
| **Craft** | Step-by-step guide for every top-tier item (Dynasty, Icarus, Moirai, Vesper): crafting order, material tree, shopping list, masterworks, a plan for your class, gear without a recipe and accessories, with where everything drops. |
| **Votes** | All vote sites with a 12-hour timer each, a beep when you can vote again and one button to open every available site. |
| **Events** | Every server event from the forum (Border Blitz, Battle Royale, Fight the Fakes, Giant Rats, Zombie Apocalypse, Lucky Pig, the Spirits, Treasure Hunt): next start in **your** time zone, how to take part and rewards. Live status (registration open, started, ended, cancelled) from the server announcements in the chat, plus the latest announcements. |
| **Timers** | Your own countdowns (instances, raids, buffs…). |
| **Options** | Language (English / Português / Deutsch), always on top, transparency, text size, auto-login and alert settings, L2Axis links and game folders. |

**Alerts** (sound + a small notice in the corner that never takes the keyboard from the game): a client
disconnected or closed by itself, a character died, HP below 30 %, MP below 10 %, an event opened registration or started.

## How it gets its information

Only from what is already on your PC and on your screen:

- **Windows itself:** the list of running programs, their windows and their network connections (to know if a client
  is connected to the server), and ping to the server.
- **Files the game writes itself:** `system/l2.log` (when the character entered the world, the region) and
  `system/WindowsInfo.ini` (where the status window is).
- **The image of the game window:** the HP/MP bars, the buttons of the login screens and the chat box. Text in the chat
  is read with the OCR that comes with Windows; only the server announcements (cyan) are used, players' chat is ignored.

It does **not** read or write the game's memory and does **not** read or change network traffic.

### What it sends to the game

Only the **auto-login**, and only if you turn it on for an account:
it types *your own* saved login and password on the login screen, presses Enter (or clicks) to confirm the server and,
if you ticked it for that account, to start with the last character played. Nothing else: no keys, clicks or
automation while you play. Passwords are encrypted with Windows (DPAPI): only your Windows user on your PC can read
them, and they are never shown again in the panel.

### About the Bosses tab

The game shows boss status in the **Alt+B → Bosses** community board. The panel reads that page from a file
(`system/bd.ini`). Writing that file requires a small client-side change that is **not included in this repository**;
it will only be published if the server staff approves it. Without it the Bosses tab stays empty.

## Installation (short version)

Windows 10/11. Full step by step with pictures: **[English](docs/GUIDE.md)** · **[Português](docs/GUIA.md)**.

1. **Code → Download ZIP** on this page and extract the folder **inside your L2Axis client folder**, next to `system`.
2. Double-click **`install.bat`** (it installs Python if needed, the only dependency and creates the Desktop shortcut).
3. Open **L2Axis Control** from your Desktop and answer **Yes** to the administrator question.

<details>
<summary>Manual installation</summary>

```
pip install -r requirements.txt
pythonw l2axis_control.pyw
```
Run it as administrator (`l2.exe` runs as administrator, so the panel needs the same to open clients and to type the
auto-login).
</details>

Your settings, timers and (encrypted) accounts are stored in `data.json` next to the script. That file is personal —
it is ignored by Git and should never be shared.

## Privacy

- No telemetry, no accounts, no internet access other than ping to the game server, opening links in your browser and
  the optional WARP toggle.
- The panel's interface is served on `127.0.0.1` only, protected by a random token generated at every start.
- The chat images used for the announcements are temporary files in your Windows `TEMP` folder, overwritten every
  few seconds and never sent anywhere.

## License

[MIT](LICENSE). The databases `data/drops_db.json` and `data/items_db.json` are derived from the
[L2J Server datapack](https://bitbucket.org/l2jserver/l2j-server-datapack) and are distributed under the
**GPL-3.0** (see [data/NOTICE.md](data/NOTICE.md)). The map regions and the Gatekeeper teleports (`data/map_db.json`) also come from that datapack. `data/l2axis_teleports.json` lists the destinations of the L2Axis Alt+B teleport as shown in game (the panel updates it when you open those pages). The world map image is not included: the panel builds it from your own client (`SysTextures/L2_RadarMap.utx`). Those are the official
High Five values: L2Axis may use its own rates and drop lists. Event schedules come from the L2Axis forum posts.
Game icons are not included: the panel reads them from your own client. Lineage II is a trademark of NCSOFT. This is
an unofficial community tool and is not affiliated with NCSOFT or with the L2Axis staff.

---

## 🇧🇷 Resumo em português

Painel flutuante para quem joga no **L2Axis**, com o visual das janelas do jogo. **Guia passo a passo:
[docs/GUIA.md](docs/GUIA.md).**

- **Clientes:** cartões por conta (até 10) com status (no jogo, região, tempo online, HP/MP), mapa dos monitores
  (clique para organizar, arraste a conta para levar a janela) e login automático opcional.
- **Avisos:** caiu/fechou, morreu, HP baixo, MP baixo, evento abrindo inscrição ou começando.
- **Eventos:** todos os eventos do servidor com o próximo horário no seu fuso, como participar e recompensas;
  status ao vivo lido dos anúncios do chat.
- **Rede, Bosses, Craft, Votos, Timers e Opções.**

**Não** lê a memória do jogo nem o tráfego de rede. Lê só o que já está na tela e nos arquivos que o próprio jogo
grava. O único envio ao jogo é o login automático que você liga: digita o seu login/senha salvos (criptografados pelo
Windows) e confirma o servidor e o personagem.
