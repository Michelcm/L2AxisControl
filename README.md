# L2Axis Control

A small floating control panel for players of the **L2Axis** (Lineage II High Five) server, styled after the game's own windows.
Made by players for the L2Axis community — mainly as an **accessibility** helper for people who play with several windows
and want ping, votes, timers and boss respawns in one place.

> 🇧🇷 **Resumo em português** no fim da página.

## Features

| Tab | What it does |
|---|---|
| **Clients** | Up to 5 slots: open, focus, minimize and close game windows, RAM/uptime per client, *Open all* and *Arrange* (tile windows). Warns before exceeding the server limit of **4 clients per HWID**. Optional auto-login (see below). |
| **Network** | Live ping and packet loss to the game server (ICMP, every 2 s) with a 2-minute graph. Cloudflare WARP status and on/off toggle. |
| **Bosses** | Respawn countdowns with absolute date/time, filters (epics / dead / ★ favorites), search, sort by respawn/level/name and level ranges. Level, area and coordinates come from the game's own data files. Beeps when a ★ boss respawns. |
| **Votes** | All 17 vote sites in one place, the 4 main ones highlighted. A 12-hour timer per site, a beep when a vote is available again, and one button to open every available site at once. |
| **Timers** | Named countdowns (raid respawns, instance cooldowns, buffs…) with presets. |
| **Options** | Language (**English / Português / Deutsch**), links (site, forum, news, donations, rules), game folders, transparency. |

The window stays on top, can be dragged by its title bar and has a compact mode that shows only ping, loss and clients.

Click the medallion in the top-left corner to open the **About** window — a small tribute to **GM RockStar**, who keeps
L2Axis alive. His avatar is included with his permission (`gm.json` + `assets/gm_*.png`).

## What it does **not** do

- It does **not** read or write the game's memory and does **not** touch network traffic.
- It does **not** play for you: no keystrokes, clicks or automation inside the game. No botting.
- Everything it shows comes from your own PC: process list, ping, and files the game itself writes.

The only interaction with a game window is optional **auto-login**: it types *your own* saved login/password into the
login screen of a client opened from the panel. Passwords are encrypted with Windows DPAPI (only your Windows user on
your PC can decrypt them) and are never shown again in the interface.

### About the Bosses tab

The game shows boss status in the **Alt+B → Bosses** community board. The panel reads that page from a file
(`system/bd.ini`). Writing that file requires a small client-side change that is **not included in this repository**;
it will only be published if the server staff approves it. Without it the Bosses tab simply stays empty.

## Installation

Requirements: **Windows 10/11**, **Python 3.11+** and the Microsoft Edge **WebView2** runtime (already present on Windows 11).

1. Put this folder **inside your L2Axis client folder**, next to the `system` folder:
   ```
   L2Axis\
   ├─ system\            (game files, l2.exe)
   └─ L2AxisControl\     (this project)
   ```
2. Install the dependency:
   ```
   pip install -r requirements.txt
   ```
3. Run the panel **as administrator** (`l2.exe` itself requires administrator rights, so the panel needs them to
   open clients and to type the auto-login):
   ```
   pythonw l2axis_control.pyw
   ```
   Tip: create a shortcut to `pythonw.exe "...\l2axis_control.pyw"` and tick *Run as administrator*.

Your settings, vote timers, boss data and (encrypted) accounts are stored in `data.json` next to the script.
That file is personal — it is ignored by Git and should never be shared.

## Privacy

- No telemetry, no accounts, no internet access other than ping to the game server, opening vote/forum links in your
  browser and the optional WARP toggle.
- The panel's UI is served on `127.0.0.1` only, protected by a random token generated at every start.

## License

[MIT](LICENSE). Lineage II is a trademark of NCSOFT. This is an unofficial community tool and is not affiliated with
NCSOFT or with the L2Axis staff.

---

## 🇧🇷 Resumo em português

Painel flutuante para quem joga no **L2Axis**, com o visual das janelas do jogo. Junta num lugar só: abrir e organizar
clientes (respeitando o limite de 4 por HWID), ping e perda de pacotes até o servidor, status dos bosses (lido da página
Alt+B → Bosses), os 17 sites de votação com timer de 12h e um botão que abre todos os disponíveis, e timers livres.
Interface em português, inglês e alemão.

**Não** lê memória nem tráfego do jogo e **não** automatiza nada dentro dele. O login automático é opcional e usa as suas
próprias senhas, criptografadas pelo Windows. Instalação: coloque a pasta dentro da pasta do cliente L2Axis,
`pip install -r requirements.txt` e rode `pythonw l2axis_control.pyw` **como administrador**.
