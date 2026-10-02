# Vérification de bout en bout du mode passerelle

`gateway_check.sh` vérifie sur un **boîtier réel** ce que les tests unitaires ne peuvent
qu'affirmer : les règles posées par le runner ferment vraiment les contournements, et
une coupure interrompt vraiment les connexions en cours. Il n'est pas lancé par
`pytest`.

## Prérequis

- Un boîtier en mode passerelle, configuré, avec un **profil de test** (par exemple
  `test`) en plage **ouverte** au moment du test.
- Un ordinateur Linux ou macOS avec `curl`, `dig` (paquet `dnsutils` ou `bind-tools`)
  et `bash`, connecté au **Wi-Fi enfants avec la clé de ce profil**. L'appareil doit
  apparaître rattaché au profil dans l'onglet Réseau.
- Un domaine **bloqué pour ce profil dans son mode actuel** (catégorie interdite par
  sa grille, ou exception « bloqué » posée pour l'occasion).

## Variables d'environnement

| Variable | Obligatoire | Rôle |
|---|---|---|
| `PROFILE` | oui | Clé du profil de test. |
| `BLOCKED_DOMAIN` | oui | Domaine bloqué pour ce profil en ce moment. |
| `KIDS_GW` | non | Adresse du boîtier côté enfants (défaut `192.168.50.1`). |
| `KIDS_IFACE` | non | Interface du Wi-Fi enfants, si l'ordinateur a aussi un câble vers la box : tout le trafic de test est alors forcé par cette interface. |
| `DASHBOARD_URL` | non | Adresse du tableau de bord **côté box** (ex. `http://192.168.0.54`). |
| `SESSION` | non | Valeur du cookie `fw_session` d'une session parent ouverte sur ce tableau de bord. |
| `LONG_URL` | non | Téléchargement long utilisé pour l'étape 6. |

Le tableau de bord **refuse toute requête venant du réseau enfants**, c'est voulu.
L'étape 6 ne peut donc utiliser l'API que si l'ordinateur joint `DASHBOARD_URL` par un
autre réseau (câble vers la box, avec `KIDS_IFACE` pour que les tests passent bien par
le Wi-Fi enfants). Sans `DASHBOARD_URL` et `SESSION`, le script demande à l'opérateur de
couper puis de rétablir le profil depuis le téléphone du parent.

## Lancer

```bash
PROFILE=test BLOCKED_DOMAIN=exemple-bloque.com bash tests/e2e/gateway_check.sh
```

Avec l'API, depuis une machine qui a aussi un câble vers la box :

```bash
PROFILE=test BLOCKED_DOMAIN=exemple-bloque.com KIDS_IFACE=wlan0 \
  DASHBOARD_URL=http://192.168.0.54 SESSION=xxxxxxxx \
  bash tests/e2e/gateway_check.sh
```

## Ce qui est vérifié

0. Internet fonctionne depuis le réseau enfants (sinon rien d'autre n'a de sens).
1. Un DNS public tapé en dur (`8.8.8.8`) reçoit la réponse de blocage : le port 53 est
   bien redirigé vers Pi-hole.
2. DNS-over-HTTPS : `cloudflare-dns.com` est refusé par son nom, et `1.1.1.1:443` et
   `8.8.8.8:443` par leur adresse.
3. DNS-over-TLS : le port 853 de `1.1.1.1` et `9.9.9.9` est refusé.
4. `use-application-dns.net`, `mask.icloud.com` et `mask-h2.icloud.com` répondent
   NXDOMAIN.
5. Aucune connectivité IPv6 sortante.
6. Pendant un téléchargement long, le profil passe en « coupé » : le téléchargement
   s'interrompt en moins de 10 s et aucune nouvelle connexion n'aboutit ; après
   annulation, la connectivité revient en moins de 60 s.

## Nettoyage

- L'étape 6 pose une dérogation « coupé » de 10 minutes et l'annule à la fin. Si le
  script s'est arrêté entre les deux, annulez-la dans l'onglet **Exceptions**.
- Retirez l'exception « bloqué » posée pour l'occasion, le cas échéant.
- Le script ne modifie rien d'autre, ni sur le boîtier ni sur l'ordinateur.
