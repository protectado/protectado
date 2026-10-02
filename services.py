# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
services.py — les services que le parent nomme, et les domaines qui les composent.

POURQUOI CE REGROUPEMENT. Un parent demande « combien de temps sur Netflix ». Netflix,
pour le réseau, c'est netflix.com, nflxvideo.net et nflxso.net. Le produit lui répondait
donc en trois lignes, et le modèle recevait la même bouillie : un journal a réellement
affiché « ytimg.com », « scdn.co » et « spotify.com » sur trois lignes pour deux services.

POURQUOI LA LISTE N'EST PLUS DANS CE FICHIER. Elle était écrite en Python, ce qui était un
défaut de nature : cette liste change parce qu'un service ajoute un domaine de diffusion,
jamais parce que le produit évolue. Elle ne fera que grandir, et la personne qui sait que
`pscp.tv` appartient à Twitch ne devrait pas avoir à ouvrir un module. Elle vit désormais
dans catalog/services.json, et ce module n'en garde que la logique.

DEUX SOURCES, par priorité décroissante :

  1. data/services.local.json — appoint LOCAL, écrit à la main sur un boîtier. Permet de
     corriger ou d'ajouter une correspondance sans attendre une publication. Ses erreurs
     sont journalisées et IGNORÉES : il est édité sur un boîtier vivant, il ne doit
     jamais l'empêcher de démarrer.
  2. catalog/services.json — catalogue LIVRÉ avec le code, toujours présent. Le boîtier
     fonctionne entièrement hors ligne avec lui.

PAS DE TROISIÈME SOURCE, et c'est un choix. Un boîtier qui irait chercher un catalogue
sur le réseau émettrait un appel sortant périodique, donc un signal disant « ce boîtier
existe, il tourne, voici son adresse ». Le produit n'en émet aucun aujourd'hui, et le
catalogue n'a pas besoin de ça : il voyage dans la publication, que l'updater va déjà
chercher. Une publication qui ne touche QUE catalog/ est appliquée sans réinstaller les
dépendances ni redémarrer les services (cf. bootstrap/protectado-update.sh), et le
rechargement se fait à chaud, au cycle suivant. C'est le cycle de mise à jour propre au
catalogue, sans second dépôt ni second canal.

MODE D'ÉCHEC, et c'est le point délicat. Le catalogue livré est STRICT : s'il est
malformé, ce module lève à l'import, donc l'agent ne démarre pas, donc l'updater fait son
rollback automatique vers le commit précédent. Le pire cas d'un fichier cassé est donc un
boîtier qui reste sur la version d'avant, ce qui est exactement ce qu'on veut. Un test
refuse de publier un catalogue invalide, pour que ce rollback ne serve jamais.

Le contraire serait dangereux : sans catalogue, plus de catégorisation immédiate, donc
tous ces domaines redeviennent « non classés », donc AUTORISÉS (cf. access_grid). Un
catalogue perdu en silence desserrerait le filtrage sans que personne ne le voie.

LES LIBELLÉS NE SE TRADUISENT PAS. « Netflix » s'écrit Netflix dans les quatre langues.
"""

import json
import logging
import os
import re

import access_grid
from paths import DATA_DIR

log = logging.getLogger("protectado.services")

_ICI = os.path.dirname(os.path.abspath(__file__))

# Catalogue livré avec le code. Toujours lu, et seul obligatoire.
SHIPPED_PATH = os.path.join(_ICI, "catalog", "services.json")

# Appoint local écrit à la main sur un boîtier. Facultatif et, contrairement au livré,
# ses erreurs ne sont jamais fatales.
LOCAL_PATH = os.path.join(DATA_DIR, "services.local.json")

# Catégories admises : celles du produit, déclarées une seule fois (cf. access_grid). Y
# mettre une liste en dur ici recréerait le doublon que ce chantier supprime.
CATEGORIES = frozenset(set(access_grid.GRID_CATEGORIES)
                       | set(access_grid.ALWAYS_BLOCKED)
                       | set(access_grid.ALWAYS_ALLOWED))

_DOMAIN_RE = re.compile(r'^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$')
_KEY_RE = re.compile(r'^[a-z0-9_]{1,40}$')

# Bornes de garde. Un catalogue venu du réseau ou édité à la main ne doit pas pouvoir
# faire gonfler la mémoire du boîtier ni ralentir chaque requête DNS.
MAX_SERVICES = 2000
MAX_DOMAINS = 20000
MAX_LABEL = 60


class CatalogError(ValueError):
    """Catalogue invalide. Fatale pour le catalogue livré, ignorée pour les autres."""


def validate(raw) -> dict:
    """Valide un catalogue et renvoie ses services. Lève CatalogError avec le motif.

    La validation est le vrai garde-fou de ce chantier : c'est elle qui permet d'accepter
    un fichier venu d'ailleurs sans lui faire confiance. Chaque message dit QUOI est
    invalide et OÙ, parce que la personne qui lira ce message éditait du JSON à la main.
    """
    if not isinstance(raw, dict):
        raise CatalogError("le catalogue doit être un objet JSON")
    services = raw.get("services")
    if not isinstance(services, dict) or not services:
        raise CatalogError("clé « services » absente ou vide")
    if len(services) > MAX_SERVICES:
        raise CatalogError(f"{len(services)} services, maximum {MAX_SERVICES}")

    propre: dict = {}
    vus: dict = {}
    total_domaines = 0
    for cle, svc in services.items():
        if not _KEY_RE.match(str(cle)):
            raise CatalogError(f"clé de service invalide : {cle!r}")
        if not isinstance(svc, dict):
            raise CatalogError(f"{cle} : doit être un objet")
        label = str(svc.get("label") or "").strip()
        if not label or len(label) > MAX_LABEL:
            raise CatalogError(f"{cle} : libellé absent ou trop long")
        categorie = str(svc.get("category") or "").strip().lower()
        if categorie not in CATEGORIES:
            raise CatalogError(f"{cle} : catégorie inconnue {categorie!r}")
        domaines = svc.get("domains")
        if not isinstance(domaines, (list, tuple)) or not domaines:
            raise CatalogError(f"{cle} : aucun domaine")
        retenus = []
        for domaine in domaines:
            d = str(domaine or "").strip().lower()
            if not _DOMAIN_RE.match(d):
                raise CatalogError(f"{cle} : domaine invalide {domaine!r}")
            # Un domaine dans deux services rendrait indéterminée l'attribution du temps
            # passé : c'est la seule contradiction que ce format permet d'écrire.
            if d in vus and vus[d] != cle:
                raise CatalogError(f"{d} appartient à la fois à {vus[d]} et à {cle}")
            vus[d] = cle
            retenus.append(d)
        total_domaines += len(retenus)
        if total_domaines > MAX_DOMAINS:
            raise CatalogError(f"plus de {MAX_DOMAINS} domaines au total")
        propre[cle] = {"label": label, "category": categorie,
                       "domains": tuple(dict.fromkeys(retenus))}
    return propre


def _lire(path: str, obligatoire: bool) -> tuple:
    """(services, version) d'un fichier. Lève si `obligatoire`, sinon renvoie ({}, "")."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        if obligatoire:
            raise CatalogError(f"catalogue livré introuvable : {path}")
        return {}, ""
    except (OSError, ValueError) as e:
        if obligatoire:
            raise CatalogError(f"{path} illisible : {e}")
        log.warning(f"catalogue d'appoint ignoré ({path}) : {e}")
        return {}, ""
    try:
        return validate(raw), str(raw.get("version") or "")
    except CatalogError as e:
        if obligatoire:
            raise
        log.warning(f"catalogue d'appoint ignoré ({path}) : {e}")
        return {}, ""


def load() -> tuple:
    """(services, versions) en appliquant la priorité des deux sources.

    La fusion se fait PAR SERVICE et non par domaine : un appoint qui redéfinit `twitch`
    remplace entièrement sa liste de domaines, au lieu de s'y ajouter. C'est le
    comportement lisible pour quelqu'un qui édite le fichier à la main, et le seul qui
    permette de RETIRER un domaine mal classé sans publier de code.
    """
    services, version = _lire(SHIPPED_PATH, obligatoire=True)
    versions = {"shipped": version}
    ajout, v = _lire(LOCAL_PATH, obligatoire=False)
    if ajout:
        services = {**services, **ajout}
        versions["local"] = v or "sans version"
    # La fusion peut avoir introduit un domaine présent dans deux services : on revalide
    # l'ensemble, et un appoint fautif est écarté en bloc plutôt qu'à moitié appliqué.
    try:
        services = validate({"services": services})
    except CatalogError as e:
        log.warning(f"fusion du catalogue d'appoint écartée : {e}")
        services, version = _lire(SHIPPED_PATH, obligatoire=True)
        versions = {"shipped": version}
    return services, versions


SERVICES, CATALOG_VERSIONS = load()

# domaine → clé de service, construit une fois pour que la recherche soit immédiate.
_BY_DOMAIN = {dom: cle for cle, svc in SERVICES.items() for dom in svc["domains"]}


def stamp() -> tuple:
    """Empreinte des fichiers sources, pour savoir s'ils ont changé sans les relire.

    C'est ce qui rend le rechargement à chaud abordable : deux appels os.stat par cycle
    du moniteur, au lieu d'ouvrir et de valider deux fichiers toutes les minutes. Un
    fichier absent compte comme 0, ce qui fait bien de son apparition un changement.
    """
    empreinte = []
    for path in (SHIPPED_PATH, LOCAL_PATH):
        try:
            empreinte.append(os.stat(path).st_mtime_ns)
        except OSError:
            empreinte.append(0)
    return tuple(empreinte)


def reload_catalog():
    """Relit les deux sources. Appelé au rechargement à chaud, et par les tests.

    NE PASSER PAR ICI QUE VIA domain_classifier.reload_services() dans le produit : la
    catégorisation immédiate est un instantané de ce catalogue, et la laisser périmée
    serait pire que ne rien recharger.

    Si le catalogue livré est devenu illisible depuis le démarrage, load() lève et les
    variables de module restent INTACTES : l'agent continue avec le catalogue qu'il avait
    en mémoire, ce qui est la seule issue sûre pour un processus déjà en service.
    """
    global SERVICES, CATALOG_VERSIONS, _BY_DOMAIN
    SERVICES, CATALOG_VERSIONS = load()
    _BY_DOMAIN = {dom: cle for cle, svc in SERVICES.items() for dom in svc["domains"]}
    return SERVICES


# ------------------------------------------------------------------ #
#  Lecture                                                            #
# ------------------------------------------------------------------ #

def service_of(domain) -> str:
    """Clé du service auquel appartient ce domaine, ou chaîne vide.

    La correspondance est EXACTE sur le domaine racine, pas par suffixe : le produit
    réduit déjà chaque requête à son domaine racine avant de l'enregistrer
    (monitor._root_domain), et un test par suffixe attraperait « faux-youtube.com ».
    """
    return _BY_DOMAIN.get(str(domain or "").strip().lower(), "")


def label_of(service) -> str:
    """Libellé lisible d'un service, ou la clé si elle n'est pas connue."""
    return (SERVICES.get(service) or {}).get("label", str(service or ""))


def domains_of(service) -> tuple:
    return tuple((SERVICES.get(service) or {}).get("domains", ()))


def category_of(service) -> str:
    return (SERVICES.get(service) or {}).get("category", "unknown")


def label_for_domain(domain) -> str:
    """Comment NOMMER un domaine au parent : son service s'il en a un, sinon lui-même.

    C'est la fonction que les rapports, le chat et les écrans doivent appeler. Elle évite
    d'écrire « scdn.co » à quelqu'un qui pense « Spotify », sans pour autant inventer un
    nom pour un domaine qu'on ne connaît pas.
    """
    service = service_of(domain)
    return label_of(service) if service else str(domain or "")


def group_by_service(counters: dict) -> dict:
    """{domaine: nombre} vers {libellé: nombre}, en additionnant les domaines d'un service.

    C'est ce qui transforme quatre lignes YouTube en une. Un domaine sans service garde
    son propre nom : on ne perd rien, on regroupe seulement ce qui va ensemble.

    N'APPLIQUER QU'À DES COMPTEURS. Des durées ne s'additionnent pas ainsi : les domaines
    d'un service sont sollicités en parallèle, donc la même séance serait comptée
    plusieurs fois (cf. database.estimate_session_minutes, qui met les horodatages en
    commun avant de découper en sessions).
    """
    out: dict = {}
    for domain, valeur in (counters or {}).items():
        cle = label_for_domain(domain)
        out[cle] = out.get(cle, 0) + (valeur or 0)
    return out


def categories_by_domain() -> dict:
    """{domaine: catégorie} pour tous les domaines connus des services.

    Sert à DÉRIVER le catalogue de catégorisation immédiate du classifieur, au lieu de le
    recopier. Deux listes des mêmes domaines finissent par diverger : c'est ainsi que
    l'entrée de fbcdn.net avait cessé d'avoir un effet sans que personne ne le voie.
    """
    return {dom: svc["category"] for svc in SERVICES.values() for dom in svc["domains"]}
