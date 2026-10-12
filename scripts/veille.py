#!/usr/bin/env python3
"""Détecte les nouveaux programmes immobiliers neufs en Île-de-France et envoie une alerte par mail.

Principe :
- on parcourt les pages listées dans config/sources.json (en respectant robots.txt et une pause entre requêtes) ;
- on garde les blocs de texte qui contiennent à la fois un déclencheur (« nouveauté », « avant-première »…),
  un mot lié aux programmes, et un code postal d'Île-de-France ;
- on ignore ce qui est déjà dans data/seen.json ;
- les nouveautés sont envoyées par mail puis enregistrées. Au tout premier passage, tout est enregistré
  sans mail (« référence initiale ») pour éviter d'envoyer des dizaines d'anciens programmes.
"""
import hashlib
import html as html_lib
import os
import re
import sys
import time
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from common import CONFIG_DIR, DATA_DIR, DOCS_DIR, load_json, now_iso, save_json, send_mail

USER_AGENT = "VeilleProgrammesFFY/1.0 (usage interne ; marcel.ambomo@flyingforyou.fr)"
TIMEOUT = 20
DELAY_SECONDES = 2  # pause minimale entre deux requêtes vers un même site

# Localisation : code postal (75012) ou numéro de département entre parenthèses (Le Blanc-Mesnil (93))
IDF_LOCALISATION = re.compile(r"\b(?:75|77|78|91|92|93|94|95)\d{3}\b|\((?:75|77|78|91|92|93|94|95)\)")
# Dates : « 12/10/2026 », « 12 octobre 2026 », « 12 oct. 2026 »
DATE_NUMERIQUE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b")
DATE_TEXTE = re.compile(r"\b(\d{1,2})\s+([A-Za-zéûèôÉ]+\.?)\s+(20\d{2})\b")
MOIS_PREFIXES = [
    ("janv", 1), ("févr", 2), ("fevr", 2), ("mars", 3), ("avr", 4), ("mai", 5),
    ("juil", 7), ("juin", 6), ("août", 8), ("aout", 8), ("sept", 9), ("oct", 10),
    ("nov", 11), ("déc", 12), ("dec", 12),
]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PREFIXES_GENERIQUES = ("contact", "commercial", "info", "accueil", "reservation", "reservations", "hello")
PAGES_CONTACT = ("/contact", "/contactez-nous", "/mentions-legales")
SELECTEUR_BLOCS = "article, li, [class*=card], [class*=programme], [class*=program], [class*=teaser]"

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
_robots = {}
_derniere_requete = {}


def autorise(url):
    """Vérifie robots.txt pour le site concerné."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=TIMEOUT)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(USER_AGENT, url)


def telecharger(url):
    """Télécharge une page en respectant robots.txt et une pause entre requêtes. Retourne le HTML ou None."""
    if not autorise(url):
        print(f"  interdit par robots.txt : {url}")
        return None
    site = urlparse(url).netloc
    attente = DELAY_SECONDES - (time.time() - _derniere_requete.get(site, 0))
    if attente > 0:
        time.sleep(attente)
    _derniere_requete[site] = time.time()
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  erreur sur {url} : {e}")
        return None
    return r.text


def normaliser(texte):
    return re.sub(r"\s+", " ", texte or "").strip()


def contient(texte, mots):
    t = texte.lower()
    return any(m.lower() in t for m in mots)


def mois_en_nombre(token):
    t = token.lower().rstrip(".")
    for prefixe, numero in MOIS_PREFIXES:
        if t.startswith(prefixe):
            return numero
    return None


def extraire_date(texte):
    """Retourne la date de publication (AAAA-MM-JJ) si le texte en contient une, sinon None.

    Les dates précédées de « livraison » (date de remise des logements) sont ignorées.
    """
    candidats = []
    for m in DATE_NUMERIQUE.finditer(texte):
        candidats.append((m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in DATE_TEXTE.finditer(texte):
        mois = mois_en_nombre(m.group(2))
        if mois:
            candidats.append((m.start(), int(m.group(3)), mois, int(m.group(1))))
    for debut, annee, mois, jour in sorted(candidats):
        if "livraison" in texte[max(0, debut - 30):debut].lower():
            continue
        try:
            return date(annee, mois, jour).isoformat()
        except ValueError:
            continue
    return None


def trouver_programmes(source, url_page, html, mots):
    """Repère les blocs qui ressemblent à une annonce de programme en Île-de-France."""
    soup = BeautifulSoup(html, "html.parser")
    trouves = []
    for bloc in soup.select(SELECTEUR_BLOCS):
        texte = normaliser(bloc.get_text(" "))
        if not 20 <= len(texte) <= 1500:
            continue
        if not contient(texte, mots["declencheurs"]):
            continue
        if not contient(texte, mots["programmes"]):
            continue
        loc = IDF_LOCALISATION.search(texte)
        if not loc:
            continue

        lien_el = bloc.find("a", href=True)
        lien = urljoin(url_page, lien_el["href"]) if lien_el else url_page
        titre = bloc.find(["h1", "h2", "h3", "h4"])
        nom = normaliser(titre.get_text()) if titre else texte[:80]
        avant = re.split(r"[.:;,!?]\s", texte[:loc.start()])[-1][-50:]
        apres = re.split(r"[.:;,!?]", texte[loc.end():])[0][:40]
        lieu = normaliser(avant + texte[loc.start():loc.end()] + apres)

        identifiant = hashlib.sha1(f"{lien}|{nom}".encode("utf-8")).hexdigest()[:16]
        trouves.append({
            "id": identifiant,
            "nom": nom,
            "lieu": lieu,
            "lien": lien,
            "date_publication": extraire_date(texte),
            "extrait": texte[:300],
        })
    return trouves


def chercher_contacts(lien, cache):
    """Cherche les adresses génériques (contact@, commercial@…) sur le site du programme."""
    p = urlparse(lien)
    site = f"{p.scheme}://{p.netloc}"
    if site in cache:
        return cache[site]

    trouves = []
    domaine = p.netloc.removeprefix("www.")
    urls = [lien] + [site + chemin for chemin in PAGES_CONTACT]
    for url in urls:
        html = telecharger(url)
        if not html:
            continue
        for adresse in EMAIL_RE.findall(html):
            adresse = adresse.lower()
            local, _, dom = adresse.partition("@")
            if dom.removeprefix("www.").endswith(domaine) and local.startswith(PREFIXES_GENERIQUES):
                if adresse not in trouves:
                    trouves.append(adresse)
        if trouves:
            break
    cache[site] = trouves
    return trouves


def construire_mail(nouveaux):
    lignes_html = []
    lignes_txt = []
    for i in nouveaux:
        contacts = ", ".join(i["contacts"]) or "non trouvé : voir la fiche"
        lignes_html.append(
            "<li>"
            f"<b>{html_lib.escape(i['nom'])}</b><br>"
            f"Lieu : {html_lib.escape(i['lieu'])}<br>"
            f"Promoteur : {html_lib.escape(i['promoteur'])}<br>"
            f"Contact : {html_lib.escape(contacts)}<br>"
            f"Détecté le : {html_lib.escape(i['detecte_le'])}<br>"
            f"Source : <a href='{html_lib.escape(i['lien'])}'>{html_lib.escape(i['lien'])}</a>"
            "</li>"
        )
        lignes_txt.append(
            f"- {i['nom']}\n  Lieu : {i['lieu']}\n  Promoteur : {i['promoteur']}\n"
            f"  Contact : {contacts}\n  Détecté le : {i['detecte_le']}\n  Source : {i['lien']}"
        )
    html = (
        f"<p>{len(nouveaux)} nouveau(x) programme(s) repéré(s) en Île-de-France :</p>"
        f"<ul>{''.join(lignes_html)}</ul>"
    )
    texte = f"{len(nouveaux)} nouveau(x) programme(s) repéré(s) :\n\n" + "\n\n".join(lignes_txt)
    return html, texte


def main():
    sources = load_json(os.path.join(CONFIG_DIR, "sources.json"), [])
    mots = load_json(os.path.join(CONFIG_DIR, "keywords.json"), {})
    seen = load_json(os.path.join(DATA_DIR, "seen.json"), {})
    programmes = load_json(os.path.join(DATA_DIR, "programmes.json"), [])

    premier_passage = not seen
    if premier_passage:
        print("Premier passage : enregistrement de la référence initiale, sans envoi de mail.")

    nouveaux = []
    ids_vus_ce_passage = set()
    cache_contacts = {}

    for source in sources:
        if not source.get("actif", True):
            continue
        print(f"Source : {source['nom']}")
        for url_page in source["pages"]:
            html = telecharger(url_page)
            if not html:
                continue
            for item in trouver_programmes(source, url_page, html, mots):
                if item["id"] in seen or item["id"] in ids_vus_ce_passage:
                    continue
                ids_vus_ce_passage.add(item["id"])
                item["source"] = source["nom"]
                item["promoteur"] = source["nom"] if source["type"] == "promoteur" else "à identifier sur la fiche"
                item["contacts"] = chercher_contacts(item["lien"], cache_contacts)
                item["detecte_le"] = now_iso()
                nouveaux.append(item)

    print(f"{len(nouveaux)} nouveau(x) programme(s) détecté(s).")
    if not nouveaux:
        return 0

    if not premier_passage:
        sujet = f"[Veille] {len(nouveaux)} nouveau(x) programme(s) en Île-de-France"
        html, texte = construire_mail(nouveaux)
        if not send_mail(sujet, html, texte):
            # Rien n'est enregistré : les programmes seront retentés au passage suivant.
            return 1

    for item in nouveaux:
        seen[item["id"]] = {"detecte_le": item["detecte_le"]}
        programmes.append(item)

    save_json(os.path.join(DATA_DIR, "seen.json"), seen)
    save_json(os.path.join(DATA_DIR, "programmes.json"), programmes)
    save_json(os.path.join(DOCS_DIR, "programmes.json"), programmes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
