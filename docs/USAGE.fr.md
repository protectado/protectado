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
> routeur des enfants : voir ci-dessous ce qu'il ferme en plus, et les
> [modes de fonctionnement](../README.fr.md#deux-modes-de-fonctionnement). Le tableau de
> bord est sur le port **80** (l'interface d'admin de Pi-hole passe sur **81**).

**Sans action parentale**, Protectado applique seul le planning configuré : couper l'accès la nuit, passer en mode travail après l'école, rouvrir en soirée.

**Sur demande**, le parent écrit dans le chat du dashboard en français naturel — l'IA interprète et agit.

### Ce que ferme le mode passerelle

En passerelle, en plus du filtrage par le nom :

- **Coupure au niveau paquet** : un appareil dont l'accès est fermé ne sort plus, et ses
  connexions déjà ouvertes tombent. Un appareil qui ne s'est connecté avec la clé d'aucun
  profil ne sort pas du tout.
- **DNS forcé** : une requête DNS envoyée à un autre serveur (8.8.8.8 réglé à la main) est
  ramenée vers le boîtier.
- **Résolveurs chiffrés refusés** : DNS-over-TLS et DNS-over-QUIC (port 853) vers
  n'importe quel serveur, et DNS-over-HTTPS vers les résolveurs publics connus, par leur
  nom et par leur adresse. La liste (`catalog/doh_resolvers.json`) se met à jour avec le
  catalogue.
- **Domaines de contournement** : `use-application-dns.net` (Firefox), `mask.icloud.com`
  et `mask-h2.icloud.com` (iCloud Private Relay) répondent « domaine inexistant ».
- **IPv6** : aucun trafic IPv6 ne transite par le réseau enfants.
- **Signalement** : chaque tentative refusée est inscrite au journal, une fois par
  appareil, par type et par jour. Une application peut le faire d'elle-même : ce n'est
  pas forcément un geste de l'enfant.

Ce qui reste possible : un **VPN**, ou un résolveur DNS-over-HTTPS absent de la liste,
fait passer le trafic hors du filtrage par le nom. Les horaires et les coupures, eux,
s'appliquent toujours. Un appareil qui envoie pendant un quart d'heure presque tout son trafic
vers une seule adresse, sans demander de noms au boîtier, est signalé au journal comme
tunnel probable (signalement seulement, sans coupure).

### Ce que le boîtier ne peut pas voir

Le boîtier ne filtre que ce qui passe par lui. Il ne voit pas :

- les **données mobiles** (4G/5G) du téléphone ;
- le **partage de connexion** d'un autre téléphone ;
- le **Wi-Fi de la box familiale**, si l'enfant en connaît la clé : son appareil peut s'y
  connecter directement. En mode passerelle, gardez cette clé pour les adultes, ou
  changez-la ;
- en mode passerelle, un **VPN** ou un résolveur chiffré absent de la liste connue fait
  encore passer le trafic hors du filtrage par le nom (les horaires et les coupures
  s'appliquent toujours). En mode DNS, un appareil réglé sur un autre DNS échappe au
  filtrage : le boîtier le détecte et le signale.

Pour ces cas, associez Protectado aux contrôles du téléphone lui-même : **Temps d'écran**
sur iPhone, **Family Link** sur Android.

---

## Premier démarrage

Au premier démarrage, Protectado choisit son mode automatiquement et ouvre un assistant
(voir [modes de fonctionnement](../README.fr.md#deux-modes-de-fonctionnement)) :

- **Mode DNS** (par défaut) — ouvrez `http://protectado.local` et définissez le **mot de
  passe parent**. C'est la seule étape ; le boîtier est alors prêt.
- **Mode passerelle** (matériel compatible) — le boîtier diffuse un Wi-Fi temporaire
  `Protectado-Setup` avec un portail captif qui vous guide pour le connecter à votre box,
  nommer le Wi-Fi des enfants et définir le mot de passe parent. Il vous demande ensuite
  de revenir sur le Wi-Fi de la maison et d'ouvrir `http://protectado.local` pour
  terminer : le réseau temporaire disparaît, et cette adresse devient celle du tableau
  de bord.

Les profils, les plannings et la clé API OpenRouter ne se saisissent pas dans l'assistant —
ils s'ajoutent ensuite depuis le tableau de bord (onglet Profils, et le panneau de chat
pour la clé). Une courte visite guidée explique chaque onglet à la première connexion.

---

## Utilisation quotidienne

### Dashboard

`http://protectado.local`  (interface d'admin Pi-hole : `http://protectado.local:81`)

Un écran par question. Réseau est une page à part, les cinq autres sont des onglets :

| Onglet | La question à laquelle il répond |
|---|---|
| **État actuel** | Que se passe-t-il maintenant, et que puis-je faire tout de suite ? |
| **Enfants** | Quelles sont les règles de cet enfant ? |
| **Exceptions** | Qu'est-ce qui déroge à ces règles en ce moment, et jusqu'à quand ? |
| **Réseau** | Qui est connecté ? |
| **Domaines** | Que fait le boîtier de ce site ? |
| **Réglages** | Le boîtier lui-même, et le journal de ce qui s'est passé. |

Le **journal des décisions** est dans Réglages. Il ne montre que des décisions :
dérogations, rallonges, blocages manuels, modifications de profil et de clé, historique
effacé, mode adulte, qu'elles soient prises à l'écran ou par l'assistant. Les tentatives
bloquées (souvent des vérifications automatiques des appareils eux-mêmes) et les
changements de plage sont dans l'**Historique** de chaque enfant.

Deux de ses lignes font exception et restent sur **État actuel**, dans une carte
« À regarder » qui n'apparaît que s'il y a quelque chose : un appareil qui contourne le
DNS du boîtier, et un appareil associé avec une clé qui n'appartient à aucun profil. Ce
sont les deux seuls cas où le filtrage ne s'applique pas du tout à quelqu'un. Une
tentative bloquée n'y figure pas : c'est le boîtier qui fait son travail, et il en arrive
des dizaines par jour. Une alerte dont la cause a disparu s'efface d'elle-même au bout de
deux jours, sans bouton à cliquer.

### Exceptions

Le boîtier accorde cinq sortes d'exceptions temporaires, et elles sont toutes ici, avec
leur échéance et un bouton pour les reprendre avant l'heure :

- une **dérogation temporaire** de quelques minutes à quelques heures ;
- une **journée entière** dans un mode donné ;
- une **rallonge** de la plage en cours (« encore 20 minutes ») ;
- un **domaine ouvert** temporairement, souvent accordé par l'assistant ;
- un **appareil sorti du filtrage** (mode adulte sur un appareil partagé).

Les trois du milieu n'étaient visibles nulle part : accordées, appliquées, puis expirées
sans que le parent puisse les voir ni les reprendre.

Les exceptions **par domaine** ne sont pas ici : elles sont permanentes, et vivent dans
l'onglet Domaines avec le catalogue qu'elles corrigent. Cet onglet ne parle que de ce qui
a une fin.

### Ouvrir l'interface de Pi-hole

Son mot de passe est généré à l'installation et n'est écrit nulle part ailleurs. Il se lit
dans **Réglages → Interface Pi-hole**, masqué par défaut et révélé par un bouton, comme la
clé Wi-Fi d'un enfant.

### Quand l'accès est coupé, le boîtier dit quand ça rouvre

Un créneau fermé ne dit pas « jusqu'à 23h59 » : il annonce l'heure de réouverture, le jour
même, le lendemain ou le jour de la semaine concerné. Une dérogation qui coupe ne promet
pas non plus le retour de l'accès à son échéance si le planning est fermé à ce moment-là.
Quand le planning n'ouvre pas dans la semaine qui vient, le boîtier le dit plutôt que
d'inventer une heure.

### Thème clair ou sombre

Le tableau de bord, l'assistant d'installation, la page de connexion et la page servie aux
enfants suivent le réglage du système : clair par défaut, sombre pour qui a réglé son
téléphone ou son ordinateur ainsi. Le bouton ☀️/🌙 de l'en-tête impose un thème et le
retient sur cet appareil, où il l'emporte sur le système dans les deux sens.

Le choix est propre au navigateur qui le fait : il ne change rien pour les autres membres
de la famille. Toutes les couleurs passent par des jetons déclarés une seule fois dans
`templates/_theme.html` ; un écran qui écrirait une couleur en dur sortirait faux dans l'un des
deux thèmes.

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
- son planning **jour par jour**, du lundi au dimanche (créneaux `off`, `homework`, `free`)
- ses overrides ponctuels (vacances, exception du soir…)

Le profil **monitoring** est spécial : il observe sans bloquer. Utile pour surveiller un appareil partagé sans lui appliquer de règles.

### Fuseau horaire

Tous les horaires du produit suivent l'heure locale du boîtier : créneaux, coucher,
dérogations temporaires, rapport du soir. Le fuseau est donc déterminant, et il est
détecté **depuis le navigateur du parent** pendant l'assistant de premier démarrage, puis
appliqué au système. Aucune géolocalisation, aucun appel à un service externe.

Il reste modifiable ensuite dans **Réglages → Heure du boîtier**. À vérifier après un déménagement, ou si le boîtier a été configuré depuis un
téléphone en déplacement : un fuseau erroné décale silencieusement toutes les règles.

---

### Une clé Wi-Fi par enfant

En posture passerelle, le boîtier diffuse un seul Wi-Fi pour les enfants, mais **chaque
profil a sa propre clé**. Ce n'est pas un détail de confort : c'est elle qui identifie
l'enfant. hostapd annonce au boîtier quelle clé a servi à l'association, donc l'appareil
reste rattaché à son profil **même quand il change d'adresse MAC**, ce que font les
téléphones récents. L'identification par adresse, elle, laissait un appareil au MAC neuf
passer pour un inconnu, donc sans filtrage ni horaires.

La clé est créée avec le profil, et se lit dans **Enfants → Modifier**, autant de fois
qu'il le faut : c'est une clé à dicter, pas un secret à usage unique. Elle se remplace au
même endroit, par une clé générée ou par la vôtre.

Changer la clé d'un enfant ne déconnecte **que ses appareils**, pas ceux des autres. Le
Wi-Fi de la box, celui des parents, n'est jamais touché.

Deux conséquences à connaître :

- un appareil qui ne connaît aucune clé **ne rejoint pas le réseau du tout**. Il n'y a
  plus d'appareil inconnu en accès libre, le refus se fait au niveau radio ;
- **tant qu'aucun profil enfant n'existe, le Wi-Fi des enfants n'est pas diffusé**. Sans
  profil, il n'y a aucune clé, et un réseau visible que personne ne peut rejoindre serait
  pire qu'un réseau absent. Il apparaît à la création du premier profil.

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

### Le catalogue des services se met à jour tout seul

Le catalogue qui relie un domaine à un service — `googlevideo.com` est YouTube,
`nflxvideo.net` est Netflix — vit dans `catalog/services.json`. C'est de la donnée, pas du
code : il change parce qu'un service ajoute un domaine de diffusion, pas parce que le
produit évolue.

Une publication qui ne touche que ce fichier est donc appliquée **sans redémarrage** : ni
réinstallation des dépendances, ni migration, ni coupure du réseau des enfants. L'agent
relit le catalogue au cycle suivant, dans la minute. Le boîtier ne va rien chercher
ailleurs : le catalogue voyage dans la mise à jour que le boîtier consulte déjà, et aucun
appel sortant supplémentaire n'est émis.

Pour corriger ou ajouter une correspondance sur **votre** boîtier sans attendre une
publication, créez `data/services.local.json` :

```json
{
  "services": {
    "arte": {"label": "Arte", "category": "education",
             "domains": ["arte.tv", "artecdn.net"]}
  }
}
```

Ce fichier se pose **par-dessus** le catalogue livré, service par service : redéfinir
`youtube` remplace sa liste de domaines entière, ce qui permet aussi d'en retirer un. Il
survit aux mises à jour, et une erreur de syntaxe y est journalisée puis ignorée, sans
empêcher le boîtier de démarrer. Les catégories admises sont celles de la grille d'accès :
`education`, `work`, `other`, `entertainment`, `social`, `adult`, `extremism`, `cdn`.

---


### Branche suivie

Un boîtier suit la branche notée dans `data/branch` (hors versionnement, donc préservée par
les mises à jour). Sans ce fichier, c'est la branche extraite localement, et en dernier
recours `stable` — celle que consomment les boîtiers en fonctionnement. Changer de branche
demande d'écrire le fichier **et** d'aligner le dépôt :

```bash
cd /opt/protectado
SVC=$(stat -c %U /opt/protectado)
echo main | sudo -u "$SVC" tee data/branch
sudo -u "$SVC" git remote set-branches origin '*'
sudo -u "$SVC" git fetch origin --depth 1 main
sudo -u "$SVC" git checkout -B main origin/main
sudo -u "$SVC" git reset --hard origin/main
```

Le `remote set-branches` est nécessaire une seule fois : l'installation clone une seule
branche, et sans lui le dépôt local ne connaît aucune autre référence distante.
`PROTECTADO_BRANCH=main sudo -E bash update.sh` force une branche pour une seule mise à
jour, sans rien épingler.

### Auto-réparation au démarrage (boîtier non configuré)

Un défaut qui empêche l'assistant de s'ouvrir empêche du même coup d'atteindre l'interface par laquelle on aurait lancé la mise à jour. Tant que le boîtier n'a pas été configuré, il rattrape donc lui-même : au démarrage, si un câble Ethernet est actif, il compare sa version à celle publiée sur la branche qu'il suit, et relance `bootstrap.sh` dans deux cas, une version plus récente existe, ou l'assistant ne répond pas alors que le code est déjà à jour.

C'est bien `bootstrap.sh` qui est relancé, et non `update.sh` : lui seul régénère `/etc/protectado/agent.json` et les unités systemd, qu'un simple alignement du code laisserait désynchronisés.

Trois tentatives infructueuses sur une même version publiée et le boîtier cesse de réessayer, pour ne pas s'acharner à chaque démarrage. Toute nouvelle publication réarme la réparation. Sans câble Ethernet, la vérification est abandonnée immédiatement et le démarrage n'est pas ralenti d'une seconde.

Sur un boîtier configuré, rien de tout cela ne s'exécute.

---

## En cas de problème

### Le navigateur ouvre une page HTTPS au lieu du tableau de bord

Le tableau de bord est servi en **HTTP**, sur `http://protectado.local`. Si vous y allez
pendant que le boîtier démarre, le `:80` ne répond pas encore : le navigateur essaie alors
le HTTPS tout seul. Jusqu'à cette version, il tombait sur l'interface d'administration de
Pi-hole, qui écoutait le 443 sans raison, avec un certificat auto-signé.

C'est corrigé : Pi-hole ne garde que son `:81` en clair, et le réglage est réappliqué à
chaque démarrage du boîtier, y compris sur un boîtier déjà installé.

Si votre navigateur insiste encore sur le HTTPS pour ce nom, c'est qu'il l'a mémorisé.
Tapez l'adresse en entier, `http://protectado.local`, ou effacez les données du site dans
ses réglages.


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

Réglages dans **Réglages → Vie privée**, et par enfant dans **Enfants**.

### Rétention

L'historique (usage quotidien, volume Internet quotidien et tunnels probables, journal d'événements, rapports IA, catalogue de domaines
non revus à la main) est conservé **90 jours par défaut**, puis effacé automatiquement
par la purge hebdomadaire. Réglable, y compris « illimité » — auquel cas rien n'est
jamais effacé, ce que l'interface signale explicitement.

> Sous 31 jours, la revue mensuelle n'a plus de matière et le dit clairement au lieu de
> produire un rapport vide ; sous 8 jours, la revue hebdomadaire fait de même.

### Effacer l'historique d'un enfant

**Enfants → Modifier → Effacer l'historique** supprime tout ce qui concerne cet enfant
— usage, timeline, événements, dérogations — en conservant sa configuration et ses
plannings. Le mot de passe parent est redemandé. Supprimer un profil propose également
d'effacer son historique, plutôt que de laisser des données sans moyen de les atteindre.

### Ce que chaque mode autorise

Un mode ne bloque pas les mêmes choses selon l'enfant. « Temps libre » ouvrait le
divertissement et les réseaux sociaux à 6 ans comme à 16, ce qui n'a de sens à aucun des
deux âges. Chaque profil a donc une **grille d'accès** : pour chaque mode, les catégories
autorisées. Tout ce qui n'y figure pas est bloqué.

Les défauts viennent de la tranche d'âge :

| Tranche | Devoirs autorise | Temps libre autorise |
|---|---|---|
| 6-9 | éducatif, travail scolaire | + sites ordinaires |
| 10-12 | éducatif, travail scolaire | + sites ordinaires, divertissement |
| 13-15 | éducatif, travail scolaire, sites ordinaires | + divertissement, réseaux sociaux |
| 16+ | éducatif, travail scolaire, sites ordinaires | + divertissement, réseaux sociaux |

13-15 et 16+ ont la même grille : à 16 ans, la différence porte sur le niveau de vie
privée, pas sur l'accès.

La grille se corrige dans **Enfants → Modifier → Personnaliser**, dans les deux sens, et
un bouton revient aux défauts de l'âge. Trois règles ne se configurent pas :

- **contenu adulte et violence sont bloqués à tout âge, dans tous les modes.** Aucun
  réglage, aucune exception ne peut les rouvrir : c'est la promesse du produit ;
- l'**infrastructure** (CDN, analytics) n'est jamais bloquée, la couper casserait les
  sites autorisés sans rien protéger ;
- un domaine **non encore classé** n'est pas bloqué. Tout domaine l'est jusqu'à sa
  classification, et bloquer l'inconnu couperait l'internet à chaque site neuf.

Les cas particuliers passent par les **exceptions par domaine**, dans l'onglet **Domaines** : autoriser nommément une chaîne éducative classée « divertissement », ou
couper un site précis que sa catégorie autorise. Une exception l'emporte sur la grille,
sauf sur les deux catégories ci-dessus. Le moteur sait viser un enfant en particulier ;
l'écran ne pose pour l'instant que des exceptions valables pour tous.

---

### Niveau de vie privée

Chaque profil a un niveau, dont la tranche d'âge n'est que le **défaut**. Les quatre
tranches du produit sont **6-9, 10-12, 13-15 et 16+**, et ce sont les seules : elles
servent aussi bien ici qu'au calibrage du ton des rapports.

| Niveau | Défaut | Ce que le parent peut reconstituer | Rapports |
|---|---|---|---|
| Détaillé | 6-9 et 10-12 | Activité par fenêtres de 5 minutes | quotidien, hebdo, mensuel |
| Résumé | 13-15 | Agrégats par demi-journée | quotidien, hebdo |
| Minimal | 16+ | Totaux du jour, sans horaire | hebdo |

**Le niveau ne change ni le blocage, ni les plannings, ni les alertes.** Il ne change que
ce qui peut être reconsulté après coup. Un parent inquiet garde accès au détail horaire
d'une journée précise : **Enfants → Modifier → Voir le détail d'une journée**. Mot de
passe redemandé, portée limitée à la date choisie, et la consultation est inscrite au
journal d'événements du parent. L'écran regroupe les fenêtres de 5 minutes en plages
continues, pour répondre à la question réellement posée : de quelle heure à quelle heure.

L'assistant conversationnel, lui, reste tenu par le niveau du profil : il annonce la
granularité dont il dispose et ne déduit aucun horaire du planning, qui dit ce qui était
autorisé et non ce qui a été utilisé.

### Ce que l'enfant peut voir

Depuis le réseau enfants, `protectado.admin` affiche à l'enfant son mode d'accès en
cours, le planning du jour, ce qui est enregistré et pour combien de temps. Cette page
n'affiche **jamais** d'historique de navigation : un frère ou une sœur y a accès depuis le
même réseau.

### Partage avec l'IA

**Réglages → Vie privée → Partager des données avec l'IA.** Désactivé, plus rien ne sort
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
    ├── claude_agent.py→ IA via OpenRouter, sur demande uniquement
    └── API Pi-hole :81 → requêtes DNS, appareils, groupes, listes de blocage
    ↓ file d'actions →
/tmp/fw-queue/
    ↓
action_runner.py (root, hors sandbox)
    → Pi-hole API (groupes, blacklists par mode)

[cron 23h — hors sandbox]
  daily_report.py → catégorisation (jusqu'à 10 passes de 60 domaines)
                  + rapport quotidien (2 appels : rapport puis résumé)
```

Le processus sous sandbox parle donc lui aussi à Pi-hole, par son API sur le port 81 :
il lit les requêtes DNS et la liste des appareils, change le groupe d'un appareil et
synchronise les listes de blocage. Le profil nono autorise ce port explicitement. Le
runner root se charge de ce que la sandbox interdit : pare-feu, point d'accès Wi-Fi,
services système.

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

La politique réseau est appliquée par **Landlock lui-même** (`nono run --sandbox-policy
landlock`), et non par le mode `auto` de nono. En `auto`, nono complète Landlock d'une
base seccomp statique pour le réseau : incapable d'exprimer une règle par port, elle
laisse passer le proxy et refuse le reste, y compris les ports que le profil autorise.
L'agent se voyait alors refuser l'écoute sur le 8080 et l'appel à l'API Pi-hole. Ce mode
exige un noyau dont l'ABI Landlock est au moins la V4, et refuse de démarrer sinon : un
service qui s'arrête en le disant vaut mieux qu'un boîtier qui tourne sans sandbox.

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
├── modes.py                  ← Le vocabulaire des modes d'accès, déclaré une fois
├── access_grid.py            ← Ce que chaque mode autorise, par tranche d'âge et par enfant
├── services.py               ← La logique de regroupement des services (la liste est
│                                dans catalog/, pas ici)
├── catalog/services.json     ← Le catalogue livré : services, libellés, domaines. De la
│                                DONNÉE, éditable sans connaître Python. Un appoint local
│                                facultatif (data/services.local.json) se pose par-dessus
├── action_runner.py          ← Exécuteur root hors sandbox
├── domain_classifier.py      ← Catégorisation domaines DNS
├── daily_report.py           ← Rapport quotidien (cron)
├── access_control.py         ← Point de passage unique des droits d'accès
├── wifi_keys.py              ← Une clé Wi-Fi par profil, servie à hostapd
├── station_identity.py       ← Qui est derrière une adresse : la clé, pas la MAC
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
    ├── admin_info.html       ← Rappel d'adresse (réseau enfants)
    ├── login.html            ← Connexion
    └── onboarding.html       ← Assistant de premier démarrage (DNS & passerelle)
```
