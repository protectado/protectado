[🇬🇧 English](USAGE.md) | [🇫🇷 Français](USAGE.fr.md) | [🇪🇸 Español](USAGE.es.md) | [🇵🇹 Português](USAGE.pt.md)

# Protectado — Usage guide and technical reference

For installation, see the [README](../README.md#getting-started) and the
[detailed installation guide](../bootstrap/INSTALL.md).

---

## How it works

```
WiFi (router)
    ↓ all DNS traffic goes through →
Pi-hole  (installed and configured by the bootstrap)
    ↓ logs + API →
Protectado  (dashboard :80 + automatic monitoring)
    ↓ DNS blocking →
Pi-hole groups by profile and mode

Every night at 11pm:
  daily report generated via OpenRouter
```

> This is **DNS mode** (the default). In **gateway mode** the box is also the children's
> router and filters at the packet level, not only DNS — see
> [operating modes](../README.md#two-operating-modes). The dashboard runs on port **80**
> (Pi-hole's own admin UI moves to **81**).

**Without any parental action**, Protectado enforces the configured schedule on its own: cutting access at night, switching to work mode after school, reopening in the evening.

**On demand**, the parent types into the dashboard chat in plain language — the AI interprets it and acts.

---

## First start

At first start Protectado picks its mode automatically and opens a setup assistant
(see [operating modes](../README.md#two-operating-modes)):

- **DNS mode** (default) — open `http://protectado.local` and set the **parent password**.
  That's the only step; the box is then ready.
- **Gateway mode** (compatible hardware) — the box hosts a temporary `Protectado-Setup`
  Wi-Fi with a captive portal that walks you through connecting it to your internet box,
  naming the children's Wi-Fi, and setting the parent password. It then asks you to go
  back to the home Wi-Fi and open `http://protectado.local` to finish: the temporary
  network disappears, and that address becomes the dashboard's.

Profiles, time schedules and the OpenRouter API key aren't entered in the assistant —
they're added afterwards from the dashboard (Profiles tab, and the chat panel for the key).
A short guided tour explains each tab on first login.

---

## Day-to-day usage

### Dashboard

`http://protectado.local`  (Pi-hole's own admin UI: `http://protectado.local:81`)

- Real-time status of each profile (active devices, current mode, next time slot)
- Event history (blocks, alerts, mode changes)
- Catalog of visited domains and their category

### Light or dark theme

The dashboard, the setup assistant, the login page and the page served to the children
follow the system setting: light by default, dark for anyone whose phone or computer is
set that way. The ☀️/🌙 button in the header forces a theme and remembers it on that
device, where it overrides the system in both directions.

The choice belongs to the browser that makes it: it changes nothing for the rest of the
family. Every colour goes through a token declared once in `templates/_theme.html`; a screen
that hardcoded a colour would come out wrong in one of the two themes.

### Parent chat

The main feature: write what you want done, the AI handles the rest.

| What you write | What it does |
|---|---|
| "Cut off internet for Alice, she needs to sleep" | Immediately blocks all her devices |
| "Allow YouTube for Alice for 30 minutes" | Unblocks youtube.com for 30 min then re-blocks |
| "Give Alice 45 more minutes tonight" | Pushes back the end of the current time slot |
| "Alice is on vacation tomorrow, free mode" | Whole day with no restrictions (except adult content) |
| "Block everything for Alice on Saturday" | Whole day blocked |
| "khanacademy.org is educational" | Recategorizes the domain — never blocked in work mode |
| "Block twitch.tv even in permissive mode" | Permanent blacklist |
| "Why was YouTube reachable yesterday afternoon?" | Explains which rule applied at that moment. How finely it answers depends on the profile's *privacy level* (see below) |

### Access modes

| Mode | What's accessible |
|---|---|
| **Blocked** | Nothing — full network cutoff |
| **Work** | Education, school tools. YouTube, social media and adult content blocked |
| **Free** | Everything except adult content |

Switching between modes is automatic based on the schedule. It can be overridden at any time from the chat or the dashboard.

---

## Profiles

Each child has their own profile with:
- their devices (fixed IPs recommended)
- their **day-by-day** schedule, Monday to Sunday (`blocked`, `work`, `permissive` time slots)
- one-off overrides (vacation, an evening exception…)

The **monitoring** profile is special: it observes without blocking. Useful for keeping an eye on a shared device without applying rules to it.

### Time zone

Every schedule in the product follows the box's local time: time slots, bedtime,
temporary overrides, the evening report. The time zone therefore matters, and it is
detected **from the parent's browser** during the first-start assistant, then applied to
the system. No geolocation, no call to an outside service.

It can be changed later under **Manage → Current mode per profile**, on the "Box time"
line. Worth checking after a move, or if the box was set up from a phone that was
travelling: a wrong time zone silently shifts every rule.

---

### Children's Wi-Fi key

In gateway posture the box broadcasts its own Wi-Fi for the children. Its key is changed
under **Manage → Children's Wi-Fi**: enter a new key (or generate a readable one), then
confirm. Useful if the key has travelled beyond the house, or if it has been forgotten —
it is never shown again, it can only be replaced.

The change disconnects **every** child device: they come back only once the new key has
been entered on each of them. The box's own Wi-Fi, the parents' one, is untouched.

---

## Adult mode on a shared device

If a child uses a shared device (TV, family tablet), the parent can temporarily switch the device to adult mode without touching the child's profile.

From the dashboard: **Adult mode** button → parent password → duration. The device automatically returns to the child's profile when the duration expires.

---

## Daily report

Every evening at 11pm, Protectado automatically sends via OpenRouter:
- the categorization of new unknown domains
- a summary of the day: time spent per domain, alerts, blocks

The report appears in the dashboard (Events section) and in the logs.

To trigger it manually:
```bash
cd /opt/protectado && .venv/bin/python daily_report.py
```

---

## Backup & Restore

The dashboard lets you back up and restore the configuration in one click.

- **Backup**: button in the dashboard → downloads a ZIP (`config.json` + database)
- **Restore**: upload the ZIP → configuration reloaded live, no restart needed

> ⚠️ The ZIP contains **secrets in plain text**: parent password, AI API key and, in gateway mode, the Wi-Fi keys. Both download and restore therefore require re-entering the parent password.

---

## Updating

```bash
cd /opt/protectado
sudo bash update.sh
```

The script fetches the latest version, migrates the database and restarts the services. The configuration (`config.json`) is never overwritten. An automatic rollback happens if the agent fails to restart correctly.

### Branch followed

A box follows the branch written in `data/branch` (outside version control, so updates keep
it). Without that file it uses the locally checked-out branch, and `stable` as a last
resort — the one running boxes consume. Switching branches means writing the file **and**
realigning the repository:

```bash
cd /opt/protectado
SVC=$(stat -c %U /opt/protectado)
echo main | sudo -u "$SVC" tee data/branch
sudo -u "$SVC" git remote set-branches origin '*'
sudo -u "$SVC" git fetch origin --depth 1 main
sudo -u "$SVC" git checkout -B main origin/main
sudo -u "$SVC" git reset --hard origin/main
```

The `remote set-branches` is needed only once: the install clones a single branch, and
without it the local repository knows no other remote reference.
`PROTECTADO_BRANCH=main sudo -E bash update.sh` forces a branch for one update only,
pinning nothing.

### Self-repair at startup (unconfigured box)

A defect that stops the setup assistant from opening also stops you from reaching the very interface you would have used to trigger an update. So as long as the box has not been configured, it catches up on its own: at startup, if an Ethernet cable is active, it compares its version with the one published on the branch it follows, and re-runs `bootstrap.sh` in two cases, a newer version exists, or the assistant does not answer even though the code is already up to date.

It is `bootstrap.sh` that runs, not `update.sh`: only the former regenerates `/etc/protectado/agent.json` and the systemd units, which merely aligning the code would leave out of sync.

After three failed attempts on the same published version the box stops retrying, so it does not grind away at every startup. Any new publication re-arms the repair. With no Ethernet cable the check is abandoned immediately and startup is not slowed by a single second.

On a configured box, none of this runs.

---

## Troubleshooting

### Restart services
```bash
sudo systemctl restart protectado-runner protectado-agent
```

### Watch what's happening live
```bash
sudo journalctl -fu protectado-agent   # dashboard + monitoring
sudo journalctl -fu protectado-runner  # Pi-hole blocking
```

### Service status
```bash
sudo systemctl status protectado-runner protectado-agent
```

## Privacy

Settings live under **Manage → Privacy**, and per profile under **Profiles**.

### Retention

History (daily usage, event log, AI reports, domains not reviewed by hand) is kept for
**90 days by default**, then deleted automatically by the weekly purge. Configurable,
including "unlimited" — in which case nothing is ever deleted, which the UI flags
explicitly.

> Below 31 days the monthly review has nothing left to work with and says so plainly
> instead of producing an empty report; below 8 days the weekly review does the same.

### Erase one child's history

**Profiles → (edit) → Erase history** removes everything about that child — usage,
timeline, events, overrides — while keeping their settings and schedules. The parent
password is required. Deleting a profile also offers to erase its history, rather than
leaving data behind with no way to reach it.

### Privacy level

Each profile has a level, for which age is only the **default**:

| Level | Default | What a parent can reconstruct | Reports |
|---|---|---|---|
| Detailed | under 13 | Activity in 5-minute windows | daily, weekly, monthly |
| Summary | 13–15 | Half-day aggregates | daily, weekly |
| Minimal | 16+ | Daily totals, no times | weekly |

**The level changes neither blocking, schedules nor alerts.** It only changes what can be
looked up afterwards. A worried parent keeps access to a specific day's hour-by-hour
detail: the password is asked again, and the lookup is written to the parent's event
log.

### What the child can see

From the children's network, `protectado.admin` shows the child their current access
mode, the day's schedule, and what is recorded and for how long. That page **never**
shows browsing history: a sibling can reach it from the same network.

### Sharing with the AI

**Manage → Privacy → Share data with the AI.** Turned off, nothing goes to OpenRouter
any more: no chat, no reports, no model-based classification. Blocking, schedules and
alerts carry on unchanged. What does go out when it is on is pseudonymised — `Child 1`,
an age range, domains and counts; never a first name, an exact age, or an IP address.

---

### Reset the database
```bash
sudo systemctl stop protectado-agent protectado-runner
cd /opt/protectado && source .venv/bin/activate
rm data/protectado.db
python -c "import database; database.init_db(); print('OK')"
sudo systemctl start protectado-runner protectado-agent
```

### Reset to reconfigure
```bash
# Show the setup assistant again (keeps values)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot
# Full factory reset (wipes config, saved Wi-Fi, detected state)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

---

## Technical reference

### Detailed architecture

```
[nono sandbox — Landlock]
  dashboard.py  (FastAPI :8080 internal — published on :80 by the root layer)
    ├── monitor.py     → 60s thread, deterministic rules, no AI
    └── claude_agent.py→ AI via OpenRouter, on demand only
    ↓ action queue →
/tmp/fw-queue/
    ↓
action_runner.py (root, outside the sandbox)
    → Pi-hole API (groups, blacklists per mode)

[cron 11pm — outside the sandbox]
  daily_report.py → classification (up to 10 passes of 60 domains)
                  + daily report (2 calls: report, then summary)
```

**Actual volume**: up to 12 OpenRouter calls on an ordinary day, 13 on Mondays (weekly
review) and 14 on the 1st of the month (monthly review). Classification passes stop as
soon as no unknown domain is left — on a settled network there is often only one or two.
A handful of calls a day on an inexpensive model: the daily cost stays low, but it is
not zero.

Routine monitoring can call the AI too, rarely: `monitor.py` records an event when an
unknown domain is seen at least 50 times in 5 minutes (`UNUSUAL_QUERY_THRESHOLD`) and
escalates to the model after 3 such events (`ESCALATE_AFTER`). With no API key, or with
AI sharing turned off, none of this leaves the box: blocking and schedules do not depend
on it.

### Security (sandbox)

The agent runs inside a Landlock sandbox (which is why the box runs Ubuntu Server — its
kernel ships Landlock). It can only access:

| Resource | Access |
|---|---|
| `/opt/protectado` | Read (`nono run --read`) |
| `/opt/protectado/data` | Read + write (config, database, state files) |
| `/tmp/fw-queue` | Write (action queue to the root runner) |
| Network — outbound | `openrouter.ai` (reports and chat) · `cloudflare-dns.com`, `security.cloudflare-dns.com`, `family.cloudflare-dns.com` (free classification of unknown domains) |
| Network — ports | 80 (dashboard), 81 (Pi-hole), 8080 (setup portal) |
| Everything else | Blocked by the kernel |

The network policy is enforced by **Landlock itself** (`nono run --sandbox-policy
landlock`), not by nono's `auto` mode. Under `auto`, nono backs Landlock with a static
seccomp baseline for networking: unable to express a per-port rule, it lets the proxy
through and refuses everything else, including the ports the profile grants. The agent
was then denied both listening on 8080 and calling the Pi-hole API. This mode needs a
kernel whose Landlock ABI is V4 or later, and refuses to start otherwise: a service that
stops and says so beats a box running with no sandbox at all.

The agent reaches neither `/var/log/pihole` nor `/etc/pihole`: it goes through the
Pi-hole API exclusively, never through its files. The profile is deployed to
`/etc/protectado/agent.json` — outside the working directory, and therefore out of the
agent's own reach.

What leaves the box, and why, is detailed in the
[Privacy section of the README](../README.md#privacy).

### Changing the AI model
In `config.json`:
```json
"openrouter": {
    "model": "anthropic/claude-sonnet-4-5"
}
```
Cheaper alternatives: `mistralai/mistral-7b-instruct`, `meta-llama/llama-3-8b-instruct`

### File structure

```
/opt/protectado/
├── data/                     ← Local data, never versioned
│   ├── config.json           ← Configuration (keys, profiles, devices)
│   ├── protectado.db         ← SQLite database (events, domains, usage)
│   ├── posture.json          ← Posture chosen at boot (gateway | dns_only)
│   ├── arp_scan.json         ← Latest ARP inventory (dns_only)
│   ├── pairing_code          ← Wizard pairing code (DNS mode)
│   └── update.trigger/.log   ← Update trigger and log
├── dashboard.py              ← Web server + monitoring (entry point)
├── monitor.py                ← DNS monitoring thread (60s)
├── claude_agent.py           ← On-demand AI via OpenRouter
├── scheduler.py              ← Time schedule per profile
├── action_runner.py          ← Root executor outside the sandbox
├── domain_classifier.py      ← DNS domain categorization
├── daily_report.py           ← Daily report (cron)
├── access_control.py         ← Single funnel for device access rights
├── device_grace.py           ← New-device grace period
├── pihole_api.py             ← Pi-hole v6 API client
├── arp_scanner.py            ← Network inventory: Pi-hole FTL, completed in dns_only
│                                by the root runner's ARP scan (data/arp_scan.json)
├── privacy.py                ← Outbound pseudonymisation, retention, privacy levels
├── database.py               ← SQLite access
├── i18n/                     ← Translations (fr, en, es, pt)
├── protectado-agent.json     ← nono sandbox profile
├── bootstrap/bootstrap.sh    ← Installation AND update
├── bootstrap/net-common.sh   ← Shared Wi-Fi country and hardware detections
├── update.sh                 ← Manual update
└── templates/
    ├── index.html            ← Dashboard
    ├── devices.html          ← Devices
    ├── admin_info.html       ← Address reminder (kids network)
    ├── login.html            ← Login
    └── onboarding.html       ← First-start setup assistant (DNS & gateway)
```
