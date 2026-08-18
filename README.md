# Protectado

[🇬🇧 English](README.md) | [🇫🇷 Français](README.fr.md) | [🇪🇸 Español](README.es.md) | [🇵🇹 Português](README.pt.md)

**Smart supervision for your family network — automatic, adaptive, considerate.**

Protectado is an open source network parental control system built for parents
of teenagers. It runs on a Raspberry Pi connected to your home network and
automatically manages your children's internet access — without you having to
manually monitor every new site or app.

---

## Why Protectado?

Teenagers browse hundreds of domains every day. Traditional parental control
tools rely on static blocklists that kids bypass in minutes. Parents don't
have time to keep up with the constantly evolving web.

Protectado solves this differently: **it learns, categorizes and blocks
dynamically**, without manual intervention. Every newly visited domain is
automatically analyzed and classified by its content. Rules apply in real
time, adapt to new platforms, and keep you informed of what's happening — so
you can have the right conversations with your child instead of chasing
workarounds.

---

## What Protectado does

- **Dynamic blocking** — Visited domains are categorized automatically, with no
  list to maintain manually. Categorization runs continuously and new rules take
  effect at the next schedule change
- **Time schedules** — Restricted access at night, work mode during
  homework, free mode on weekends — set once, enforced automatically
- **Daily reports** — Smart, natural-language summary of your child's
  digital day
- **Contextual alerts** — Detection of DNS bypass attempts, unusual
  patterns, concerning content
- **AI agent** — Ask questions in plain language and get clear answers.
  Give instructions: "block TikTok for Alice", "allow Signal tonight"
- **Full visibility** — Real-time dashboard per device and per child

---

## What Protectado is not

Protectado observes network browsing patterns — not the content of private
messages or your children's conversations. It operates at the DNS level: it
knows your child visited YouTube, not what they watched there.

The goal isn't total surveillance but **a healthy, predictable digital
lifestyle** — clear rules, automatically enforced, that still leave room for
trust and dialogue.

---

## Architecture

Protectado is built on [Pi-hole](https://pi-hole.net) as its DNS engine,
enriched with an AI layer for classification and analysis.

### Two operating modes

Protectado runs in one of two modes, picked **automatically** at first start — you
don't choose, it adapts to what it's installed on:

- **DNS mode** (default) — Protectado filters DNS on your existing network. Run it on a
  Raspberry Pi or on any always-on Linux machine you already have (a NAS, a mini-PC).
  Your router points devices to it for name resolution. Works everywhere, no extra
  hardware.
- **Gateway mode** (advanced) — Protectado becomes the children's router: it broadcasts
  a dedicated Wi-Fi for them and filters **every** connection, not just DNS — much harder
  to bypass. This mode needs **compatible hardware**.

By default the box installs in **DNS mode**, and switches to gateway mode on its own only
when it detects compatible hardware.

### Hardware

| Mode | Hardware | Capabilities |
|------|----------|--------------|
| **DNS** (default) | Any Raspberry Pi (2W / 3 / 4 / 5) or an always-on Linux machine | Dynamic DNS blocking, AI reports, schedules |
| **Gateway** (advanced) | Raspberry Pi 4 / 5 with compatible Wi-Fi hardware | The above **+** packet-level filtering and a dedicated, filtered Wi-Fi for the children |

> The box installs in DNS mode by default and switches to gateway mode on its own when it
> detects compatible hardware.

### Software components
```
protectado-client    This repo — runs on your Raspberry Pi
protectado-server    Central server (shared, anonymous classification) — planned
protectado.com       Website and documentation
```

---

## Getting started

Three steps, just like on [protectado.com](https://protectado.com): plug in,
configure, forget. The path to get there depends on your plan.

### Community (free, self-hosted)

1. **Plug in** — flash an SD card with Ubuntu Server (64-bit) and connect the
   Pi to your home network. Full step-by-step guide: [bootstrap/INSTALL.md](bootstrap/INSTALL.md)
2. **Install** — connect via SSH and run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
   ```
   Automatic installation of Pi-hole and Protectado (5 to 10 minutes).
3. **Configure** — a setup assistant opens on its own at first start. In **DNS mode**
   open `http://protectado.local` and set your parent password; in **gateway mode** the
   box hosts a temporary `Protectado-Setup` Wi-Fi that walks you through connecting it to
   your internet box and naming the children's Wi-Fi. Profiles and schedules are added
   afterwards from the dashboard.

> **Requirements**: Raspberry Pi (2W, 3, 4 or 5) or any always-on Linux machine ·
> Ubuntu Server recommended (the AI agent runs in a Landlock sandbox) · Connection to
> your home network

---

## Privacy

Filtering happens at the **DNS level**: Protectado sees the *names* of the sites
requested, never their content, never messages, never passwords. Nothing is streamed to
a central Protectado server — there is none.

Two things do leave the box, both of them optional:

| What leaves | Where to | When | What it contains |
|---|---|---|---|
| Domain names | Cloudflare DoH resolvers (`1.1.1.1`, `1.1.1.2`, `1.1.1.3`) | Classifying an unknown domain | The domain name alone — no profile, no device, no time |
| Pseudonymised usage data | OpenRouter (the AI model you choose) | Reports and parent chat, **only if you configure an API key** | `Child 1`, an age *range* (`13-15`), domains and counts. Never a first name, an exact age, an IP or MAC address, or a raw event message |

**The AI is entirely optional.** Without an API key, Protectado blocks, schedules,
alerts and reports device activity exactly the same — you simply get no written reports
and no chat. You can also keep the key and switch sharing off at any time under
**Manage → Privacy**.

**Retention.** History is kept for 90 days by default, then deleted automatically —
configurable, including "unlimited" if you deliberately choose it. Each child's history
can be erased individually at any time, and each profile has a *privacy level* that
limits how finely their past activity can be reconstructed. Blocking, schedules and
safety alerts are never affected by that level.

**Children can see this too.** From the children's network, `protectado.admin` shows the
child their current access mode, the day's schedule, what is recorded and for how long —
and tells them when a parent has looked at their detailed history.

---

## Documentation

For day-to-day usage (chat commands, access modes, backup/restore) and
technical reference (sandbox security, file structure), see
[docs/USAGE.md](docs/USAGE.md).

---

## License

Protectado is available under two licenses:

- **Personal / open source use**: GNU AGPL v3 — see [LICENSE](LICENSE)
- **Commercial use**: Protectado Commercial License —
  see [LICENSE-COMMERCIAL](LICENSE-COMMERCIAL) · arnaud@barbed.fr

Copyright (C) 2026 Arnaud Ortais

Protectado uses [Pi-hole](https://pi-hole.net), licensed under EUPL v1.2.
