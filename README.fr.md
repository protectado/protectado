[🇬🇧 English](README.md) | [🇫🇷 Français](README.fr.md) | [🇪🇸 Español](README.es.md) | [🇵🇹 Português](README.pt.md)

# Protectado

**Supervision intelligente du réseau familial — automatique, adaptative, bienveillante.**

Protectado est un système de contrôle parental réseau open source conçu pour les
parents d'adolescents. Il tourne sur un Raspberry Pi branché à votre réseau
domestique et gère automatiquement l'accès internet de vos enfants — sans que
vous ayez à surveiller manuellement chaque nouveau site ou application.

---

## Pourquoi Protectado ?

Les adolescents naviguent sur des centaines de domaines par jour. Les outils de
contrôle parental traditionnels s'appuient sur des listes statiques que les enfants
contournent en quelques minutes. Les parents n'ont pas le temps de suivre
l'évolution constante du web.

Protectado règle ce problème différemment : **il apprend, catégorise et bloque
dynamiquement**, sans intervention manuelle. Chaque nouveau domaine visité est
analysé automatiquement et classé selon son contenu. Les règles s'appliquent
en temps réel, s'adaptent aux nouvelles plateformes, et vous informent de ce
qui se passe — pour que vous puissiez avoir les bonnes conversations avec
votre enfant plutôt que de courir après les contournements.

---

## Ce que Protectado fait

- **Blocage dynamique** — Les domaines visités sont catégorisés automatiquement,
  sans liste à maintenir manuellement. La catégorisation tourne en continu et les
  nouvelles règles prennent effet au changement de plage horaire suivant
- **Plannings horaires** — Accès restreint la nuit, mode travail pendant les
  devoirs, mode libre le weekend — configurés une fois, appliqués automatiquement
- **Rapports quotidiens** — Synthèse intelligente en langage naturel de la journée
  numérique de votre enfant
- **Alertes contextuelles** — Détection des tentatives de contournement DNS,
  des patterns inhabituels, des contenus préoccupants
- **Agent IA** — Posez des questions en français et obtenez des réponses claires.
  Donnez des instructions : "bloque TikTok pour Alice", "autorise Signal ce soir"
- **Visibilité complète** — Tableau de bord temps réel par appareil et par enfant

---

## Ce que Protectado n'est pas

Protectado observe les patterns de navigation réseau — pas le contenu des
messages privés ni les conversations de vos enfants. Il agit au niveau du DNS :
il sait que votre enfant a visité YouTube, pas ce qu'il y a regardé.

L'objectif n'est pas la surveillance totale mais **un cadre de vie numérique
sain et prévisible** — des règles claires, appliquées automatiquement, qui
laissent de la place pour la confiance et le dialogue.

---

## Architecture

Protectado repose sur [Pi-hole](https://pi-hole.net) comme moteur DNS, enrichi
d'une couche d'intelligence artificielle pour la classification et l'analyse.

### Deux modes de fonctionnement

Protectado fonctionne dans l'un de deux modes, choisi **automatiquement** au premier
démarrage — vous ne choisissez pas, il s'adapte à ce sur quoi il est installé :

- **Mode DNS** (par défaut) — Protectado filtre le DNS sur votre réseau existant.
  Installez-le sur un Raspberry Pi ou sur n'importe quelle machine Linux déjà allumée en
  permanence (un NAS, un mini-PC). Votre box y dirige les appareils pour la résolution de
  noms. Fonctionne partout, sans matériel supplémentaire.
- **Mode passerelle** (avancé) — Protectado devient le routeur des enfants : il diffuse un
  Wi-Fi dédié pour eux et filtre **chaque** connexion, pas seulement le DNS — bien plus
  difficile à contourner. Ce mode nécessite un **matériel compatible**.

Par défaut, le boîtier s'installe en **mode DNS**, et ne bascule en mode passerelle de
lui-même que s'il détecte un matériel compatible.

### Matériel

| Mode | Matériel | Capacités |
|------|----------|-----------|
| **DNS** (par défaut) | N'importe quel Raspberry Pi (2W / 3 / 4 / 5) ou une machine Linux allumée en permanence | Blocage DNS dynamique, rapports IA, plannings |
| **Passerelle** (avancé) | Raspberry Pi 4 / 5 avec un matériel Wi-Fi compatible | Ce qui précède **+** filtrage au niveau paquet et un Wi-Fi dédié et filtré pour les enfants |

> Le boîtier s'installe en mode DNS par défaut et bascule en mode passerelle de lui-même
> lorsqu'il détecte un matériel compatible.

### Composants logiciels
```
protectado-client    Ce dépôt — tourne sur votre Raspberry Pi
protectado-server    Serveur central (classification partagée, anonyme) — prévu
protectado.com       Site web et documentation
```

---

## Démarrage

Trois étapes, comme sur [protectado.com](https://protectado.com) : brancher, configurer, oublier.
Le chemin pour y arriver dépend de votre offre.

### Community (gratuit, auto-hébergé)

1. **Brancher** — flashez une carte SD avec Ubuntu Server (64 bits) et branchez le Pi
   à votre réseau domestique. Guide pas à pas complet : [bootstrap/INSTALL.fr.md](bootstrap/INSTALL.fr.md)
2. **Installer** — connectez-vous en SSH et lancez :
   ```bash
   curl -fsSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
   ```
   Installation automatique de Pi-hole et Protectado (5 à 10 minutes).
3. **Configurer** — un assistant s'ouvre tout seul au premier démarrage. En **mode DNS**,
   ouvrez `http://protectado.local` et définissez votre mot de passe parent ; en **mode
   passerelle**, le boîtier diffuse un Wi-Fi temporaire `Protectado-Setup` qui vous guide
   pour le connecter à votre box et nommer le Wi-Fi des enfants. Les profils et les
   plannings se créent ensuite depuis le tableau de bord.

> **Prérequis** : Raspberry Pi (2W, 3, 4 ou 5) ou toute machine Linux allumée en
> permanence · Ubuntu Server recommandé (l'agent IA tourne dans une sandbox Landlock) ·
> Connexion à votre réseau domestique

---

## Vie privée

Le filtrage se fait **au niveau DNS** : Protectado voit les *noms* des sites demandés,
jamais leur contenu, jamais les messages, jamais les mots de passe. Rien n'est remonté
vers un serveur central Protectado — il n'en existe pas.

Deux choses sortent du boîtier, toutes deux optionnelles :

| Ce qui sort | Vers qui | Quand | Ce que ça contient |
|---|---|---|---|
| Noms de domaine | Résolveurs Cloudflare DoH (`1.1.1.1`, `1.1.1.2`, `1.1.1.3`) | Classification d'un domaine inconnu | Le nom de domaine seul — pas de profil, pas d'appareil, pas d'horaire |
| Données d'usage pseudonymisées | OpenRouter (le modèle d'IA que vous choisissez) | Rapports et chat parent, **uniquement si vous configurez une clé API** | `Enfant 1`, une *tranche* d'âge (`13-15`), des domaines et des compteurs. Jamais de prénom, d'âge exact, d'adresse IP ou MAC, ni de message d'événement en clair |

**L'IA est entièrement optionnelle.** Sans clé API, Protectado bloque, planifie, alerte
et suit l'activité exactement de la même façon — vous n'avez simplement ni rapports
rédigés ni chat. Vous pouvez aussi garder la clé et couper le partage à tout moment dans
**Gestion → Vie privée**.

**Rétention.** L'historique est conservé 90 jours par défaut, puis effacé
automatiquement — réglable, y compris « illimité » si vous le choisissez délibérément.
L'historique de chaque enfant peut être effacé individuellement à tout moment, et chaque
profil dispose d'un *niveau de vie privée* qui limite la finesse avec laquelle son
activité passée peut être reconstituée. Le blocage, les plannings et les alertes de
sécurité ne sont jamais affectés par ce niveau.

**L'enfant aussi peut le voir.** Depuis le réseau enfants, `protectado.admin` lui affiche
son mode d'accès en cours, le planning de la journée, ce qui est enregistré et pour
combien de temps — et l'informe lorsqu'un parent a consulté le détail de son historique.

---

## Documentation

Pour l'usage quotidien (commandes chat, modes d'accès, backup/restore) et la
référence technique (sécurité sandbox, structure des fichiers), voir
[docs/USAGE.fr.md](docs/USAGE.fr.md).

---

## Licence

Protectado est disponible sous deux licences :

- **Usage personnel / open source** : GNU AGPL v3 — voir [LICENSE](LICENSE)
- **Usage commercial** : Licence Commerciale Protectado —
  voir [LICENSE-COMMERCIAL](LICENSE-COMMERCIAL) · arnaud@barbed.fr

Copyright (C) 2026 Arnaud Ortais

Protectado utilise [Pi-hole](https://pi-hole.net), licencié sous EUPL v1.2.
