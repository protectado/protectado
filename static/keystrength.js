// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Arnaud Ortais
//
// keystrength.js — Estimation de la solidité d'une clé Wi-Fi saisie à la main.
//
// À QUOI ÇA SERT. La clé proposée par le boîtier vaut 65 bits : hors de portée d'une
// attaque hors ligne, même pour qui connaît le code. Mais le champ est modifiable, et
// un parent qui la remplace par « maison2024 » annule tout ce travail sans le savoir.
// Ce fichier sert à le lui dire — AVERTIR, jamais bloquer : le parent reste maître de
// la clé de son propre réseau.
//
// CE QUE L'ESTIMATION VAUT. Elle majore : le calcul part de la taille de l'alphabet
// utilisé, puis retire ce que des motifs évidents rendent devinable (répétitions,
// suites de clavier, bases de mots de passe courantes). Une clé jugée faible l'est
// certainement ; une clé jugée solide peut l'être moins que le chiffre ne le dit — un
// mot rare d'un dictionnaire y ressemble à du hasard. C'est suffisant pour l'usage :
// on cherche à rattraper les clés manifestement faibles, pas à noter les autres.
//
// Partagé par l'assistant de première installation et le tableau de bord : une seule
// règle, un seul endroit à faire évoluer.
(function () {
  var COMMON = [
    "password", "passwd", "motdepasse", "contrasena", "palavrapasse", "azerty",
    "qwerty", "qwertz", "123456", "abc123", "admin", "administrateur", "wifi",
    "internet", "maison", "casa", "home", "family", "famille", "familia",
    "enfant", "enfants", "kids", "protectado", "livebox", "freebox", "bbox",
    "orange", "sfr", "bouygues", "movistar", "vodafone", "iloveyou", "soleil"
  ];

  // Suites triviales : un attaquant les essaie avant tout le reste.
  function hasRun(s) {
    for (var i = 0; i + 3 < s.length; i++) {
      var up = true, down = true, same = true;
      for (var k = 1; k < 4; k++) {
        var d = s.charCodeAt(i + k) - s.charCodeAt(i + k - 1);
        if (d !== 1) up = false;
        if (d !== -1) down = false;
        if (d !== 0) same = false;
      }
      if (up || down || same) return true;
    }
    return false;
  }

  // Le mot le plus court répété pour former toute la chaîne (« abab » → « ab »).
  function period(s) {
    for (var p = 1; p <= s.length / 2; p++) {
      if (s.length % p) continue;
      var ok = true;
      for (var i = p; i < s.length; i++) if (s[i] !== s[i - p]) { ok = false; break; }
      if (ok) return p;
    }
    return s.length;
  }

  // Estimation « choix humain » : une personne qui invente une clé produit environ
  // 2 bits par caractère, pas les 4,7 d'un tirage au sort — elle choisit des mots, des
  // dates, des prénoms. On retient TOUJOURS la plus basse des deux estimations : se
  // tromper en rassurant est le seul échec qui compte ici.
  function humanBits(key) {
    var b = 4 + Math.max(0, key.length - 1) * 2;
    if (/[a-z]/.test(key) && /[A-Z]/.test(key)) b += 6;
    if (/[0-9]/.test(key)) b += 6;
    if (/[^A-Za-z0-9]/.test(key)) b += 8;
    return b;
  }

  // `generated` : la clé que le boîtier vient de proposer. Tant que le parent la garde,
  // rien à estimer — sa force est connue exactement (cf. passphrase.py), et aucune
  // heuristique ne doit venir la mettre en doute.
  function strength(key, generated) {
    key = String(key || "");
    if (!key) return { bits: 0, level: "weak" };
    if (generated && key === generated) return { bits: 65, level: "strong" };

    var pool = 0;
    if (/[a-z]/.test(key)) pool += 26;
    if (/[A-Z]/.test(key)) pool += 26;
    if (/[0-9]/.test(key)) pool += 10;
    if (/[^A-Za-z0-9]/.test(key)) pool += 33;

    // Longueur utile : une clé entièrement répétitive ne vaut que son motif.
    var useful = period(key);
    var bits = useful * (Math.log(pool || 1) / Math.LN2);

    // Peu de caractères distincts : l'alphabet théorique n'est pas celui employé.
    var distinct = new Set(key.split("")).size;
    if (distinct <= 4) bits = Math.min(bits, distinct * 4);

    if (hasRun(key)) bits -= 12;

    // Base courante : le mot ne compte plus, seul le reste apporte quelque chose.
    var low = key.toLowerCase();
    for (var i = 0; i < COMMON.length; i++) {
      if (low.indexOf(COMMON[i]) !== -1) {
        bits -= COMMON[i].length * (Math.log(pool || 1) / Math.LN2);
        bits -= 2;                       // le fait d'être dans une liste courte
        break;
      }
    }

    bits = Math.max(0, Math.round(Math.min(bits, humanBits(key))));
    // 40 bits : la limite en dessous de laquelle une capture WPA2 se force en quelques
    // heures sur du matériel loué. 60 : au-delà, plus rien d'accessible aujourd'hui.
    var level = bits < 40 ? "weak" : (bits < 60 ? "fair" : "strong");
    return { bits: bits, level: level };
  }

  window.ptKeyStrength = strength;
})();
