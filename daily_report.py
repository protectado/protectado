#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""Lancé par cron à 23h — catégorise les domaines + génère le rapport."""
import os, json, sys
from datetime import date as _date
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import domain_classifier as classifier
import claude_agent
import database as db
from paths import CONFIG_PATH

print(f"\n{'='*48}\n Rapport Protectado — {_date.today()}\n{'='*48}")

# 1. Charger la config
with open(CONFIG_PATH) as f:
    config = json.load(f)

# 0. Purger la timeline DNS. La rétention fine reste courte (7 jours) car c'est la table
#    la plus intrusive — horodatage à la seconde — mais elle ne dépasse jamais la
#    politique de rétention globale choisie par le parent.
import privacy
_retention = privacy.retention_days(config)
db.purge_old_timeline(days=min(7, _retention) if _retention else 7)

# 2. Catégoriser les domaines inconnus (Cloudflare + Claude)
print("=== Catégorisation des domaines ===")
results = classifier.classify_with_claude(config)
for domain, cat in results.items():
    print(f"  {domain} → {cat}")

# 3. Rapport quotidien (1 appel Claude)
print("\n=== Rapport quotidien ===")
report = claude_agent.daily_report()
print(report)

if report.startswith("Erreur"):
    sys.exit(1)

# 4. Revue hebdomadaire chaque lundi
if _date.today().weekday() == 0:
    print("\n=== Revue hebdomadaire ===")
    print(claude_agent.weekly_report())

# 5. Revue mensuelle le 1er de chaque mois
if _date.today().day == 1:
    print("\n=== Revue mensuelle ===")
    print(claude_agent.monthly_report())
