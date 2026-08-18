[🇬🇧 English](INSTALL.md) | [🇫🇷 Français](INSTALL.fr.md) | [🇪🇸 Español](INSTALL.es.md) | [🇵🇹 Português](INSTALL.pt.md)

# Protectado — Installation Guide

This guide covers the complete installation of Protectado at a new family's home, from a blank SD card to an operational dashboard.

---

## Installation on an existing Linux machine (NAS, old PC...)

If you already have a Linux machine on the family network — a NAS, mini-PC, or old PC running Ubuntu — the bootstrap works directly on it.

**Requirements:**
- Debian / Ubuntu (the script uses `apt`)
- The machine must be on the **same local network** as the children's devices
- Pi-hole v6 already installed, **or** not yet installed (the bootstrap installs it)
- Python 3.10 minimum (`python3 --version`)
- systemd active

> **VPS / remote server: not compatible.** Pi-hole must see local DNS traffic. A cloud server cannot play this role without a VPN.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

If Pi-hole is already installed and configured, the bootstrap detects it and leaves it intact — it only installs Protectado on top. If Pi-hole is absent, it installs it.

Continue from **Step 4** below (wizard configuration).

---

## Installation on Raspberry Pi (nominal path)

---

## What to prepare BEFORE going to the family's home

### Hardware

| Item | Notes |
|------|-------|
| Raspberry Pi | Pi 3B+, Pi 4 or Pi 5 recommended (built-in Ethernet). Pi 2W works over WiFi. |
| SD card | 16 GB minimum, class 10 |
| Power supply | USB-C (Pi 4/5) or micro-USB (Pi 2W/3) |
| Ethernet cable | Optional but recommended — plugs the Pi directly into the router |

### Accounts / keys to create in advance

**OpenRouter API key** (essential — AI will not work without it)
1. Create an account at [openrouter.ai](https://openrouter.ai)
2. Add credit (a few euros lasts several months)
3. Generate an API key → copy the key (starts with `sk-or-`)

---

## Step 1 — Prepare the SD card (on your PC)

1. Download **Raspberry Pi Imager**: [raspberrypi.com/software](https://www.raspberrypi.com/software/)
2. Insert the SD card into your PC
3. In Raspberry Pi Imager:
   - **Device** → choose your Pi model
   - **Operating system** → `Ubuntu Server (64-bit)` (needed by the AI-agent sandbox)
   - **Storage** → your SD card
4. Click **⚙️ Edit settings** (before flashing!)

In the advanced settings, configure:

```
✅ Hostname          → protectado
✅ Enable SSH         → Use a password
   Username           → pi
   Password           → [choose an SSH password]
✅ Configure WiFi     → [household SSID and password]
   WiFi country       → [your country]
```

> **If using an Ethernet cable**: you can leave WiFi unconfigured.
> The Pi will get its IP automatically via the cable.

5. Flash the card → insert into the Pi

---

## Step 2 — First boot

1. Plug in the Ethernet cable **or** let WiFi connect automatically
2. Plug in the power supply
3. Wait ~60 seconds (the Pi boots and joins the network)

**Find the Pi's IP address:**

```bash
# Option A — from your PC on the same network
ping protectado.local

# Option B — router admin interface (often 192.168.1.1)
# Look for "protectado" or "raspberrypi" in the connected devices list
```

---

## Step 3 — SSH connection and installation

```bash
ssh pi@protectado.local
# (or ssh pi@192.168.x.x with the IP found above)
```

Once connected, run the installation with a single command:

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

Installation takes **5 to 10 minutes**. It automatically installs:
- Pi-hole (DNS filtering)
- Protectado (AI agent + dashboard)
- Automatic updates

At the end, the script displays:

```
╔══════════════════════════════════════════════════╗
║        Protectado installed successfully!       ║
╚══════════════════════════════════════════════════╝

  Dashboard  →  http://192.168.x.x

  ┌─ Configuration information ──────────────────────
  │  PIHOLE_PASSWORD :  xxxxxxxxxxxxxxxx
  │  PAIRING_CODE    :  XXXXXXXX
  └──────────────────────────────────────────────────
```

**Keep the Pi-hole password** — for the Pi-hole admin UI, reachable at `http://<ip>:81`.

**Keep the pairing code** — in DNS mode the wizard asks for it before accepting the parent
password. Until the box is configured it answers the whole home network: without this code any
device — including a child's — could set the parent password before you do. In gateway mode the
wizard is only reachable from the isolated `Protectado-Setup` network and the code is not asked.

---

## Step 4 — First-start setup assistant

At first boot Protectado picks its **mode automatically** and opens a setup assistant.
You don't choose the mode — it depends on the hardware
(see [operating modes](../README.md#two-operating-modes)).

### DNS mode (default)

From any device on the network, open `http://protectado.local` (or the IP from step 3).
The assistant asks for the dashboard **parent password**, then shows a **required step**.

> ⚠️ **In DNS mode, nothing is filtered until your router sends devices to the box.**
> The Pi is not a router: it only sees devices that ask it to resolve site names.

In your router's interface (often `http://192.168.1.1`), look for **DNS** in the network or
DHCP settings, and replace the DNS server with the box address — the assistant displays it.
Then restart Wi-Fi on your children's devices so they pick it up.

If your router does not allow changing the DNS, set it device by device in their Wi-Fi
settings. Profiles and schedules are added from the dashboard afterwards.

### Gateway mode (compatible hardware detected)

The box broadcasts a temporary open Wi-Fi named **`Protectado-Setup`**. Connect a phone to
it — a captive portal opens on its own — and follow the steps:

| Step | What to enter |
|------|---------------|
| 1 | Your internet box — pick your Wi-Fi network and enter **its** key |
| 2 | Children's Wi-Fi — a name and an easy 3-word key (WPA2) |
| 3 | Parent password for the dashboard |
| 4 | Reconnect the phone to your box, open the shown address, click **Finish** |

The box then reboots into gateway mode: the children's Wi-Fi goes live and filtered, and
the setup network disappears. The dashboard stays reachable at `http://<box-ip>`.

> The **OpenRouter API key** is entered later, from the dashboard's chat panel — not in
> this assistant.

---

## Step 5 — Assign devices to profiles

In the dashboard → **Devices** tab:

1. Click **Scan network**
2. For each detected device: select the profile from the dropdown
3. Click **Assign**

> ⚠️ **The ZIP contains secrets in PLAIN TEXT**: the parent password, the AI API key and, in gateway mode, your router's Wi-Fi key and the children's network key. Downloading and restoring therefore require re-entering the parent password. Keep the file as you would keep those passwords: never on shared storage, never sent by email.

> **Tip**: turn on the children's phones/tablets so they appear in the scan.

---

## Step 6 — Configure time slots

In the dashboard → **Profiles** tab:

1. Click **Edit** on a profile
2. Add time slots for each day of the week (the old Weekday/Weekend format is still
   read for backward compatibility)
3. Available modes: `blocked` (all cut), `work` (educational only), `permissive` (open access)
4. Click **Save**
5. Click **⚙️ Reconfigure Pi-hole** to apply the groups

---

## Backup & Restore

In the dashboard → **Management** tab → **Backup & Restore** card:

| Action | Description |
|--------|-------------|
| ⬇️ Download | Generates a ZIP containing `config.json` (profiles, schedule, API keys) and the SQLite database |
| ⬆️ Restore | Imports a previously downloaded ZIP — configuration is reloaded immediately without restart |

> **Tip**: back up before each manual update and after any significant profile changes.

---

## Troubleshooting

**Pi not appearing on the network**
- Wait an extra 2 minutes
- Check that the SSID/WiFi password is correct (redo step 1)
- Try with an Ethernet cable

**Dashboard not opening**
```bash
sudo systemctl status protectado-agent
sudo journalctl -u protectado-agent -n 30
```

**Pi-hole not accessible**
```bash
pihole status
sudo systemctl restart pihole-FTL
```

**Manual update**
```bash
sudo bash /opt/protectado/update.sh
```

---

## Reset to reconfigure

To run the setup assistant again (e.g. hand the box to another family):

```bash
# Soft reset — keep the values, just show the assistant again at next boot
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot

# Full reset — factory state: wipes config, saved Wi-Fi and detected state
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

After a full reset the box comes back like new and re-picks its mode (DNS or gateway)
automatically at boot.

---

## Automatic updates

Protectado updates itself every night at 3am from the **`stable`** branch.

`main` is the development branch; `stable` is promoted **manually**. A regression pushed
in the evening therefore cannot break every box by morning. The branch chosen at install
time is recorded in `data/branch` and reused by the updater: a box never changes branch
on its own.

```bash
# Promote the current state of main to stable (from the dev repository)
git checkout stable && git merge --ff-only main && git push origin stable

# Install a test machine on main instead of stable
curl -sSL .../bootstrap.sh | sudo PROTECTADO_BRANCH=main bash
```

The installed version (short commit, branch, date) is shown in the dashboard →
**Manage** tab → **Update** card, and on the login page.
Pi-hole updates every Sunday at 4am.
OS security patches install automatically via `unattended-upgrades`.

---

## Updating an existing installation

The bootstrap script automatically detects an existing Protectado installation and switches to update mode instead of reinstalling.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

What the update does:
1. Saves `config.json` and `protectado.db` to a timestamped backup in `/opt/`
2. Pulls the latest code from the branch this box follows
3. Restores `config.json` (your profiles and configuration are preserved)
4. Runs database migrations (`database.init_db()`)
5. Restarts the services

If the agent fails to start after the update, the script automatically rolls back to the saved backup.
