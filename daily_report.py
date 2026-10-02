#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""Lancé par cron à 23h — catégorise les domaines + génère le rapport.

Chaque étape est indépendante : l'échec de l'une (fournisseur IA injoignable, délai
dépassé) n'empêche pas les suivantes, en particulier les revues hebdomadaire et
mensuelle, qui n'ont qu'un soir pour être produites. Le code de sortie est non nul si
une étape a échoué, pour que cron et les journaux le montrent.
"""
import os, json, sys
from datetime import date as _date

if __name__ == "__main__":
    # Avant les imports, comme toujours : les chemins relatifs du produit partent du dépôt.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

import domain_classifier as classifier
import claude_agent
import database as db
import privacy
from paths import CONFIG_PATH


def _etape(nom: str, fn, echecs: list):
    """Exécute une étape, note son échec sans l'interrompre. Une réponse qui commence
    par « Erreur » est l'échec tel que claude_agent le rend, sans exception."""
    print(f"\n=== {nom} ===")
    try:
        resultat = fn()
    except Exception as e:
        print(f"Échec : {e}")
        echecs.append(nom)
        return None
    if isinstance(resultat, str):
        print(resultat)
        if resultat.startswith("Erreur"):
            echecs.append(nom)
    return resultat


def main(today: _date | None = None) -> int:
    today = today or _date.today()
    print(f"\n{'='*48}\n Rapport Protectado — {today}\n{'='*48}")
    echecs: list = []

    # 1. Charger la config
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # 0. Purger la timeline DNS. La rétention fine reste courte (7 jours) car c'est la
    #    table la plus intrusive — horodatage à la seconde — mais elle ne dépasse jamais
    #    la politique de rétention globale choisie par le parent.
    _retention = privacy.retention_days(config)
    db.purge_old_timeline(days=min(7, _retention) if _retention else 7)

    # 2. Catégoriser les domaines inconnus (Cloudflare + Claude)
    def categoriser():
        for domain, cat in classifier.classify_with_claude(config).items():
            print(f"  {domain} → {cat}")
    _etape("Catégorisation des domaines", categoriser, echecs)

    # 3. Rapport quotidien
    _etape("Rapport quotidien", claude_agent.daily_report, echecs)

    # 4. Revue hebdomadaire chaque lundi
    if today.weekday() == 0:
        _etape("Revue hebdomadaire", claude_agent.weekly_report, echecs)

    # 5. Revue mensuelle le 1er de chaque mois
    if today.day == 1:
        _etape("Revue mensuelle", claude_agent.monthly_report, echecs)

    if echecs:
        print(f"\nÉtapes en échec : {', '.join(echecs)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
