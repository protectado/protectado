[🇬🇧 English](USAGE.md) | [🇫🇷 Français](USAGE.fr.md) | [🇪🇸 Español](USAGE.es.md) | [🇵🇹 Português](USAGE.pt.md)

# Protectado — Guide d'utilisation et référence technique

Pour l'installation, voir le [README](../README.fr.md#démarrage) et le
[guide d'installation détaillé](../bootstrap/INSTALL.fr.md).

---

## Comment ça marche

```
WiFi (box, routeur)
    ↓ tout le trafic DNS passe par →
Pi-hole  (installé et configuré par le bootstrap)
    ↓ logs + API →
Protectado  (dashboard :80 + surveillance automatique)
    ↓ blocages DNS →
groupes Pi-hole par profil et par mode

Chaque nuit à 23h :
  rapport quotidien généré via OpenRouter
```

> Ceci est le **mode DNS** (par défaut). En **mode passerelle**, le boîtier est aussi le
> routeur des enfants et filtre au niveau paquet, pas seulement le DNS — voir
> [modes de fonctionnement](../README.fr.md#deux-modes-de-fonctionnement). Le tableau de
> bord est sur le port **80** (l'interface d'admin de Pi-hole passe sur **81**).

**Sans action parentale**, Protectado applique seul le planning configuré : couper l'accès la nuit, passer en mode travail après l'école, rouvrir en soirée.

**Sur demande**, le parent écrit dans le chat du dashboard en français naturel — l'IA interprète et agit.

---

## Premier démarrage

Au premier démarrage, Protectado choisit son mode automatiquement et ouvre un assistant
(voir [modes de fonctionnement](../README.fr.md#deux-modes-de-fonctionnement)) :

- **Mode DNS** (par défaut) — ouvrez `http://protectado.local` et définissez le **mot de
  passe parent**. C'est la seule étape ; le boîtier est alors prêt.
- **Mode passerelle** (matériel compatible) — le boîtier diffuse un Wi-Fi temporaire
  `Protectado-Setup` avec un portail captif qui vous guide pour le connecter à votre box,
  nommer le Wi-Fi des enfants et définir le mot de passe parent.

Les profils, les plannings et la clé API OpenRouter ne se saisissent pas dans l'assistant —
ils s'ajoutent ensuite depuis le tableau de bord (onglet Profils, et le panneau de chat
pour la clé). Une courte visite guidée explique chaque onglet à la première connexion.

---

## Utilisation quotidienne

### Dashboard

`http://protectado.local`  (interface d'admin Pi-hole : `http://protectado.local:81`)

- Statut en temps réel de chaque profil (appareils actifs, mode en cours, plage suivante)
- Historique des événements (blocages, alertes, changements de mode)
- Catalogue des domaines visités et leur catégorie

### Chat parent

La fonctionnalité principale : écrire ce qu'on veut faire, l'IA s'occupe du reste.

| Ce que vous écrivez | Ce que ça fait |
|---|---|
| "Coupe internet à Alice, elle doit dormir" | Bloque immédiatement tous ses appareils |
| "Autorise YouTube pour Alice pendant 30 minutes" | Débloque youtube.com 30 min puis rebloque |
| "Donne 45 minutes de plus à Alice ce soir" | Repousse la fin du créneau actuel |
| "Demain Alice est en vacances, mode libre" | Journée entière sans restriction (sauf adulte) |
| "Bloque tout pour Alice samedi" | Journée entière bloquée |
| "khanacademy.org c'est éducatif" | Recatégorise le domaine — jamais bloqué en mode travail |
| "Bloque twitch.tv même en mode permissif" | Blacklist permanente |
| "Pourquoi YouTube était accessible hier après-midi ?" | Explique quelle règle s'appliquait à ce moment-là. La finesse de la réponse dépend du *niveau de vie privée* du profil (voir ci-dessous) |

### Modes d'accès

| Mode | Ce qui est accessible |
|---|---|
| **Bloqué** | Rien — coupure réseau complète |
| **Travail** | Éducation, outils scolaires. YouTube, réseaux sociaux et contenus adultes bloqués |
| **Libre** | Tout sauf les contenus adultes |

Le passage d'un mode à l'autre est automatique selon le planning. Il peut être surchargé à tout moment depuis le chat ou le dashboard.

---

## Profils

Chaque enfant a son propre profil avec :
- ses appareils (IP fixes recommandées)
- son planning **jour par jour**, du lundi au dimanche (créneaux `blocked`, `work`, `permissive`)
- ses overrides ponctuels (vacances, exception du soir…)

Le profil **monitoring** est spécial : il observe sans bloquer. Utile pour surveiller un appareil partagé sans lui appliquer de règles.

### Fuseau horaire

Tous les horaires du produit suivent l'heure locale du boîtier : créneaux, coucher,
dérogations temporaires, rapport du soir. Le fuseau est donc déterminant, et il est
détecté **depuis le navigateur du parent** pendant l'assistant de premier démarrage, puis
appliqué au système. Aucune géolocalisation, aucun appel à un service externe.

Il reste modifiable ensuite dans **Gestion → Mode actuel par profil**, ligne « Heure du
boîtier ». À vérifier après un déménagement, ou si le boîtier a été configuré depuis un
téléphone en déplacement : un fuseau erroné décale silencieusement toutes les règles.

---

## Mode adulte sur appareil partagé

Si un enfant utilise un appareil partagé (TV, tablette familiale), le parent peut basculer temporairement l'appareil en mode adulte sans toucher au profil de l'enfant.

Depuis le dashboard : bouton **Mode adulte** → mot de passe parent → durée. L'appareil revient automatiquement dans le profil enfant à l'expiration.

---

## Rapport quotidien

Chaque soir à 23h, Protectado envoie automatiquement via OpenRouter :
- la catégorisation des nouveaux domaines inconnus
- un résumé de la journée : temps passé par domaine, alertes, blocages

Le rapport apparaît dans le dashboard (section Événements) et dans les logs.

Pour le déclencher manuellement :
```bash
cd /opt/protectado && .venv/bin/python daily_report.py
```

---

## Backup & Restore

Le dashboard permet de sauvegarder et restaurer la configuration en un clic.

- **Backup** : bouton dans le dashboard → télécharge un ZIP (`config.json` + base de données)
- **Restore** : uploader le ZIP → configuration rechargée à chaud, sans redémarrage

> ⚠️ Le ZIP contient **des secrets en clair** : mot de passe parent, clé de l'API IA et, en mode passerelle, les clés Wi-Fi. Le téléchargement comme la restauration demandent donc une nouvelle saisie du mot de passe parent.

---

## Mise à jour

```bash
cd /opt/protectado
sudo bash update.sh
```

Le script récupère la dernière version, migre la base de données et redémarre les services. La configuration (`config.json`) n'est jamais écrasée. Un rollback automatique est effectué si l'agent ne redémarre pas correctement.

---

## En cas de problème

### Redémarrer les services
```bash
sudo systemctl restart protectado-runner protectado-agent
```

### Voir ce qui se passe en direct
```bash
sudo journalctl -fu protectado-agent   # dashboard + surveillance
sudo journalctl -fu protectado-runner  # blocages Pi-hole
```

### Statut des services
```bash
sudo systemctl status protectado-runner protectado-agent
```

## Vie privée

Réglages dans **Gestion → Vie privée**, et par profil dans **Profils**.

### Rétention

L'historique (usage quotidien, journal d'événements, rapports IA, catalogue de domaines
non revus à la main) est conservé **90 jours par défaut**, puis effacé automatiquement
par la purge hebdomadaire. Réglable, y compris « illimité » — auquel cas rien n'est
jamais effacé, ce que l'interface signale explicitement.

> Sous 31 jours, la revue mensuelle n'a plus de matière et le dit clairement au lieu de
> produire un rapport vide ; sous 8 jours, la revue hebdomadaire fait de même.

### Effacer l'historique d'un enfant

**Profils → (modifier) → Effacer l'historique** supprime tout ce qui concerne cet enfant
— usage, timeline, événements, dérogations — en conservant sa configuration et ses
plannings. Le mot de passe parent est redemandé. Supprimer un profil propose également
d'effacer son historique, plutôt que de laisser des données sans moyen de les atteindre.

### Niveau de vie privée

Chaque profil a un niveau, dont l'âge n'est que le **défaut** :

| Niveau | Défaut | Ce que le parent peut reconstituer | Rapports |
|---|---|---|---|
| Détaillé | < 13 ans | Activité par fenêtres de 5 minutes | quotidien, hebdo, mensuel |
| Résumé | 13–15 ans | Agrégats par demi-journée | quotidien, hebdo |
| Minimal | ≥ 16 ans | Totaux du jour, sans horaire | hebdo |

**Le niveau ne change ni le blocage, ni les plannings, ni les alertes.** Il ne change que
ce qui peut être reconsulté après coup. Un parent inquiet garde accès au détail horaire
d'une journée précise : mot de passe redemandé, et la consultation est inscrite au
journal d'événements — visible par le parent, et par l'enfant sur sa propre page.

### Ce que l'enfant peut voir

Depuis le réseau enfants, `protectado.admin` affiche à l'enfant son mode d'accès en
cours, le planning du jour, ce qui est enregistré et pour combien de temps, et si un
parent a consulté le détail de son historique. Cette page n'affiche **jamais** d'historique
de navigation : un frère ou une sœur y a accès depuis le même réseau.

### Partage avec l'IA

**Gestion → Vie privée → Partager des données avec l'IA.** Désactivé, plus rien ne sort
vers OpenRouter : ni chat, ni rapports, ni classification par le modèle. Le blocage, les
plannings et les alertes continuent à l'identique. Ce qui sort quand c'est activé est
pseudonymisé — `Enfant 1`, une tranche d'âge, des domaines et des compteurs ; jamais de
prénom, d'âge exact ni d'adresse IP.

---

### Réinitialiser la base de données
```bash
sudo systemctl stop protectado-agent protectado-runner
cd /opt/protectado && source .venv/bin/activate
rm data/protectado.db
python -c "import database; database.init_db(); print('OK')"
sudo systemctl start protectado-runner protectado-agent
```

### Réinitialiser pour reconfigurer
```bash
# Réafficher l'assistant (garde les valeurs)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot
# Reset total sortie d'usine (efface config, Wi-Fi enregistré, état détecté)
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

---

## Référence technique

### Architecture détaillée

```
[nono sandbox — Landlock]
  dashboard.py  (FastAPI :8080 interne — publié sur :80 par la couche root)
    ├── monitor.py     → thread 60s, règles déterministes sans IA
    └── claude_agent.py→ IA via OpenRouter, sur demande uniquement
    ↓ file d'actions →
/tmp/fw-queue/
    ↓
action_runner.py (root, hors sandbox)
    → Pi-hole API (groupes, blacklists par mode)

[cron 23h — hors sandbox]
  daily_report.py → catégorisation (jusqu'à 10 passes de 60 domaines)
                  + rapport quotidien (2 appels : rapport puis résumé)
```

**Volume réel** : jusqu'à 12 appels OpenRouter un jour ordinaire, 13 le lundi (revue
hebdomadaire) et 14 le 1er du mois (revue mensuelle). Les passes de catégorisation
s'arrêtent dès qu'il n'y a plus de domaine inconnu — sur un réseau stabilisé, il n'en
reste souvent qu'une ou deux. Quelques appels par jour sur un modèle bon marché : le
coût quotidien reste faible, mais il n'est pas nul.

La surveillance courante peut elle aussi solliciter l'IA, rarement : `monitor.py` empile
un événement quand un domaine inconnu est vu au moins 50 fois en 5 minutes
(`UNUSUAL_QUERY_THRESHOLD`) et escalade vers le modèle au bout de 3 événements
(`ESCALATE_AFTER`). Sans clé API, ou avec le partage IA désactivé, rien de tout cela ne
part : le blocage et les plannings n'en dépendent pas.

### Sécurité (sandbox)

L'agent tourne dans un sandbox Landlock (c'est pourquoi le boîtier tourne sous Ubuntu
Server — son noyau embarque Landlock). Il ne peut accéder qu'à :

| Ressource | Accès |
|---|---|
| `/opt/protectado` | Lecture (`nono run --read`) |
| `/opt/protectado/data` | Lecture + écriture (config, base, fichiers d'état) |
| `/tmp/fw-queue` | Écriture (file d'actions vers le runner root) |
| Réseau — sortant | `openrouter.ai` (rapports et chat) · `cloudflare-dns.com`, `security.cloudflare-dns.com`, `family.cloudflare-dns.com` (classification gratuite des domaines inconnus) |
| Réseau — ports | 80 (tableau de bord), 81 (Pi-hole), 8080 (portail de configuration) |
| Tout le reste | Bloqué par le kernel |

L'agent n'accède ni à `/var/log/pihole` ni à `/etc/pihole` : il passe exclusivement par
l'API de Pi-hole, jamais par ses fichiers. Le profil est déployé dans
`/etc/protectado/agent.json` — hors du répertoire de travail, donc hors de portée de
l'agent lui-même.

Le détail de ce qui sort du boîtier, et pourquoi, est dans la section
[Vie privée du README](../README.fr.md#vie-privée).

### Changer le modèle IA
Dans `config.json` :
```json
"openrouter": {
    "model": "anthropic/claude-sonnet-4-5"
}
```
Alternatives économiques : `mistralai/mistral-7b-instruct`, `meta-llama/llama-3-8b-instruct`

### Structure des fichiers

```
/opt/protectado/
├── data/                     ← Données locales, jamais versionnées
│   ├── config.json           ← Configuration (clés, profils, appareils)
│   ├── protectado.db         ← Base SQLite (événements, domaines, usage)
│   ├── posture.json          ← Posture retenue au boot (gateway | dns_only)
│   ├── arp_scan.json         ← Dernier inventaire ARP (dns_only)
│   ├── pairing_code          ← Code d'appairage de l'assistant (mode DNS)
│   └── update.trigger/.log   ← Déclencheur et journal de mise à jour
├── dashboard.py              ← Serveur web + surveillance (point d'entrée)
├── monitor.py                ← Thread de surveillance DNS (60s)
├── claude_agent.py           ← IA à la demande via OpenRouter
├── scheduler.py              ← Planning horaire par profil
├── action_runner.py          ← Exécuteur root hors sandbox
├── domain_classifier.py      ← Catégorisation domaines DNS
├── daily_report.py           ← Rapport quotidien (cron)
├── access_control.py         ← Point de passage unique des droits d'accès
├── device_grace.py           ← Délai de grâce des nouveaux appareils
├── pihole_api.py             ← Client API Pi-hole v6
├── arp_scanner.py            ← Inventaire réseau : Pi-hole FTL, complété en dns_only
│                                par le scan ARP du runner root (data/arp_scan.json)
├── privacy.py                ← Pseudonymisation des sorties, rétention, niveaux
├── database.py               ← Accès SQLite
├── i18n/                     ← Traductions (fr, en, es, pt)
├── protectado-agent.json     ← Profil sandbox nono
├── bootstrap/bootstrap.sh    ← Installation ET mise à jour
├── bootstrap/net-common.sh   ← Pays Wi-Fi et détection matérielle partagés
├── update.sh                 ← Mise à jour manuelle
└── templates/
    ├── index.html            ← Dashboard
    ├── devices.html          ← Appareils
    ├── admin_info.html       ← Rappel d'adresse (réseau enfants)
    ├── login.html            ← Connexion
    └── onboarding.html       ← Assistant de premier démarrage (DNS & passerelle)
```
