#!/usr/bin/env python3
"""Veille des programmes immobiliers neufs en Île-de-France.

Trois types de sources (champ « format » dans config/sources.json) :

- « liste » : pages de résultats par département (SeLoger Neuf). Chaque fiche affichée est repérée
  avec son département, et une mention éventuelle (« Nouveauté », « Lancement commercial »…).
- « catalogue » : plan du site listant des fiches (Trouver un logement neuf). Chaque fiche est lue,
  et seules celles situées en Île-de-France sont gardées. Par passage, MAX_FICHES_CATALOGUE fiches
  au plus sont lues, pour rester raisonnable ; le reste est traité les jours suivants.
- « promoteur » : site d'un promoteur, filtré par mots-clés (« nouveauté », « exclusivité »…).

Règles d'alerte :
- seules les fiches « liste » repérées APRÈS le premier passage déclenchent un mail ;
- le premier passage et les fiches « catalogue » sont enregistrés sans mail : ils apparaissent
  sur la page avec la mention « Catalogue ».
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
MAX_FICHES_CATALOGUE = 150  # fiches lues par passage pour le plan du site

DEPARTEMENTS_IDF = ("75", "77", "78", "91", "92", "93", "94", "95")
LOC_IDF = re.compile(r"\b(?:75|77|78|91|92|93|94|95)\d{3}\b|\((?:75|77|78|91|92|93|94|95)\)")
RE_FICHE_LISTE = re.compile(r"/programme/([a-z0-9-]+)-(\d{2})/(\d+)/?")
MENTIONS = (
    "Nouveauté", "Lancement commercial", "Lancement exceptionnel", "Offre spéciale",
    "Emménagement immédiat", "Retour de lots", "Travaux en cours",
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PREFIXES_GENERIQUES = ("contact", "commercial", "info", "accueil", "reservation", "reservations", "hello")
PAGES_CONTACT = ("/contact", "/contactez-nous", "/mentions-legales")
SELECTEUR_BLOCS = "article, li, [class*=card], [class*=programme], [class*=program], [class*=teaser]"
DATE_NUMERIQUE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b")
DATE_TEXTE = re.compile(r"\b(\d{1,2})\s+([A-Za-zéûèôÉ]+\.?)\s+(20\d{2})\b")
MOIS_PREFIXES = [
    ("janv", 1), ("févr", 2), ("fevr", 2), ("mars", 3), ("avr", 4), ("mai", 5),
    ("juin", 6), ("juil", 7), ("août", 8), ("aout", 8), ("sept", 9), ("oct", 10),
    ("nov", 11), ("déc", 12), ("dec", 12),
]
PETITS_MOTS = {"sur", "sous", "en", "les", "la", "le", "de", "du", "des", "lès", "et"}

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
_robots = {}
_derniere_requete = {}


# --- Accès aux sites -------------------------------------------------------------------------

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
    """Télécharge une page en respectant robots.txt et une pause entre requêtes. Retourne le texte ou None."""
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


# --- Outils de texte et de date --------------------------------------------------------------

def normaliser(texte):
    return re.sub(r"\s+", " ", texte or "").strip()


def contient(texte, mots):
    t = texte.lower()
    return any(m.lower() in t for m in mots)


def identifiant(*morceaux):
    return hashlib.sha1("|".join(morceaux).encode("utf-8")).hexdigest()[:16]


def nom_ville(slug):
    """« villiers-sur-marne » -> « Villiers sur Marne »."""
    mots = slug.split("-")
    return " ".join(
        m if (i and m in PETITS_MOTS) else m.capitalize() for i, m in enumerate(mots)
    )


def mois_en_nombre(token):
    t = token.lower().rstrip(".")
    for prefixe, numero in MOIS_PREFIXES:
        if t.startswith(prefixe):
            return numero
    return None


def extraire_date(texte):
    """Date de publication (AAAA-MM-JJ) si le texte en contient une, sinon None.

    Les dates proches du mot « livraison » (remise des logements) sont ignorées.
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


# --- Lecture des sources ----------------------------------------------------------------------

def texte_de_la_carte(lien, repli):
    """Remonte depuis le lien jusqu'au plus grand bloc qui ne contient qu'une seule fiche."""
    def numeros_dans(noeud):
        trouves = set()
        for x in noeud.select('a[href*="/programme/"]'):
            m = RE_FICHE_LISTE.search(x.get("href", ""))
            if m:
                trouves.add(m.group(3))
        return trouves

    noeud = lien
    for _ in range(6):
        parent = noeud.parent
        if parent is None or len(numeros_dans(parent)) > 1:
            break
        noeud = parent
    texte = normaliser(noeud.get_text(" "))
    return texte if len(texte) <= 1500 else repli


def trouver_listing(url_page, html):
    """Format « liste » : fiches d'une page de résultats par département."""
    soup = BeautifulSoup(html, "html.parser")
    fiches = {}
    stats = {"liens_programme": 0, "hors_idf": 0}
    for a in soup.select('a[href*="/programme/"]'):
        m = RE_FICHE_LISTE.search(a["href"])
        if not m:
            continue
        slug, dept, numero = m.groups()
        if dept not in DEPARTEMENTS_IDF:
            stats["hors_idf"] += 1
            continue
        stats["liens_programme"] += 1
        fiche = fiches.setdefault(numero, {
            "slug": slug, "dept": dept, "nom": "", "texte": "",
            "href": urljoin(url_page, a["href"]).split("?")[0],
        })
        lien_texte = normaliser(a.get_text(" "))
        if len(lien_texte) > len(fiche["nom"]):
            fiche["nom"] = lien_texte
        if not fiche["texte"]:
            fiche["texte"] = texte_de_la_carte(a, lien_texte)

    trouves = []
    for numero, f in fiches.items():
        mention = next((m for m in MENTIONS if m.lower() in f["texte"].lower()), "")
        nom = f["nom"] or f"Programme {nom_ville(f['slug'])}"
        trouves.append({
            "id": identifiant("selogerneuf", numero),
            "nom": nom,
            "lieu": f"{nom_ville(f['slug'])} ({f['dept']})",
            "lien": f["href"],
            "mention": mention,
            "date_publication": None,
            "extrait": f["texte"][:300],
        })
    stats["idf"] = len(trouves)
    return trouves, stats


def trouver_promoteur(url_page, html, mots):
    """Format « promoteur » : blocs de texte contenant un déclencheur, un mot « programme » et un département IDF."""
    soup = BeautifulSoup(html, "html.parser")
    trouves = []
    stats = {"blocs": 0, "avec_declencheur": 0, "avec_mot_programme": 0, "avec_localisation": 0}
    for bloc in soup.select(SELECTEUR_BLOCS):
        texte = normaliser(bloc.get_text(" "))
        if not 20 <= len(texte) <= 1500:
            continue
        stats["blocs"] += 1
        if not contient(texte, mots["declencheurs"]):
            continue
        stats["avec_declencheur"] += 1
        if not contient(texte, mots["programmes"]):
            continue
        stats["avec_mot_programme"] += 1
        loc = LOC_IDF.search(texte)
        if not loc:
            continue
        stats["avec_localisation"] += 1

        lien_el = bloc.find("a", href=True)
        lien = urljoin(url_page, lien_el["href"]) if lien_el else url_page
        titre = bloc.find(["h1", "h2", "h3", "h4"])
        nom = normaliser(titre.get_text()) if titre else texte[:80]
        avant = re.split(r"[.:;,!?]\s", texte[:loc.start()])[-1][-50:]
        apres = re.split(r"[.:;,!?]", texte[loc.end():])[0][:40]
        trouves.append({
            "id": identifiant(lien, nom),
            "nom": nom,
            "lieu": normaliser(avant + texte[loc.start():loc.end()] + apres),
            "lien": lien,
            "mention": "",
            "date_publication": extraire_date(texte),
            "extrait": texte[:300],
        })
    return trouves, stats


def trouver_catalogue(url_sitemap, deja_connus):
    """Format « catalogue » : lit les fiches non encore connues et garde celles situées en IDF.

    Retourne (fiches_idf, toutes_les_cles_lues, stats). Les fiches hors IDF sont aussi retenues
    dans toutes_les_cles_lues pour ne pas les relire le lendemain.
    """
    stats = {"urls_dans_plan": 0, "deja_connues": 0, "lues": 0, "idf": 0, "sans_localisation": 0, "echec": 0}
    texte_plan = telecharger(url_sitemap)
    if not texte_plan:
        stats["echec"] += 1
        return [], [], stats

    urls = [l.strip() for l in texte_plan.splitlines() if l.strip().startswith("http")]
    stats["urls_dans_plan"] = len(urls)
    a_lire = []
    for u in urls:
        if identifiant("catalogue", u) in deja_connus:
            stats["deja_connues"] += 1
        else:
            a_lire.append(u)

    idf, lues = [], []
    for u in a_lire[:MAX_FICHES_CATALOGUE]:
        cle = identifiant("catalogue", u)
        page = telecharger(u)
        if not page:
            stats["echec"] += 1
            continue  # non marquée : elle sera retentée le lendemain
        stats["lues"] += 1
        lues.append(cle)
        soup = BeautifulSoup(page, "html.parser")
        h1 = soup.find("h1")
        titre = normaliser(h1.get_text(" ")) if h1 else ""
        loc = LOC_IDF.search(titre)
        if not loc:
            stats["sans_localisation"] += 1
            continue
        stats["idf"] += 1
        fenetre = normaliser(titre[max(0, loc.start() - 40):loc.end() + 5])
        idf.append({
            "id": cle,
            "nom": titre.split(" - ")[0][:120] or u.rsplit("/", 1)[-1],
            "lieu": fenetre,
            "lien": u,
            "mention": "",
            "date_publication": None,
            "extrait": titre[:300],
        })
    return idf, lues, stats


# --- Contacts et mails ------------------------------------------------------------------------

def chercher_contacts(lien, cache):
    """Cherche les adresses génériques (contact@, commercial@…) sur le site du promoteur."""
    p = urlparse(lien)
    site = f"{p.scheme}://{p.netloc}"
    if site in cache:
        return cache[site]

    trouves = []
    domaine = p.netloc.removeprefix("www.")
    for url in [lien] + [site + chemin for chemin in PAGES_CONTACT]:
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
    lignes_html, lignes_txt = [], []
    for i in nouveaux:
        contacts = ", ".join(i.get("contacts", [])) or "non trouvé : voir la fiche"
        mention = f"Mention : {i['mention']}" if i.get("mention") else ""
        lignes_html.append(
            "<li>"
            f"<b>{html_lib.escape(i['nom'])}</b><br>"
            f"Lieu : {html_lib.escape(i['lieu'])}<br>"
            f"Promoteur : {html_lib.escape(i['promoteur'])}<br>"
            + (f"{html_lib.escape(mention)}<br>" if mention else "")
            + f"Contact : {html_lib.escape(contacts)}<br>"
            f"Détecté le : {html_lib.escape(i['detecte_le'])}<br>"
            f"Source : <a href='{html_lib.escape(i['lien'])}'>{html_lib.escape(i['lien'])}</a>"
            "</li>"
        )
        lignes_txt.append(
            f"- {i['nom']}\n  Lieu : {i['lieu']}\n  Promoteur : {i['promoteur']}\n"
            + (f"  {mention}\n" if mention else "")
            + f"  Contact : {contacts}\n  Détecté le : {i['detecte_le']}\n  Source : {i['lien']}"
        )
    html = (
        f"<p>{len(nouveaux)} nouveau(x) programme(s) repéré(s) en Île-de-France :</p>"
        f"<ul>{''.join(lignes_html)}</ul>"
    )
    texte = f"{len(nouveaux)} nouveau(x) programme(s) repéré(s) :\n\n" + "\n\n".join(lignes_txt)
    return html, texte


# --- Passage principal ------------------------------------------------------------------------

def main():
    sources = load_json(os.path.join(CONFIG_DIR, "sources.json"), [])
    mots = load_json(os.path.join(CONFIG_DIR, "keywords.json"), {})
    seen = load_json(os.path.join(DATA_DIR, "seen.json"), {})
    programmes = load_json(os.path.join(DATA_DIR, "programmes.json"), [])

    premier_passage = not seen
    if premier_passage:
        print("Premier passage : enregistrement de la référence initiale, sans envoi de mail.")

    maintenant = now_iso()
    rapport = {"date": maintenant, "premier_passage": premier_passage, "sources": []}
    ajouts = []          # fiches nouvelles à enregistrer
    alertes = []         # fiches nouvelles qui déclenchent un mail
    marques_vues = {}    # clés lues (même hors IDF) pour ne pas les relire
    ids_vus = set()
    cache_contacts = {}

    for source in sources:
        if not source.get("actif", True):
            continue
        fmt = source.get("format", "promoteur")
        print(f"Source : {source['nom']} ({fmt})")

        if fmt == "catalogue":
            for url_plan in source["pages"]:
                fiches, lues, stats = trouver_catalogue(url_plan, seen)
                print(f"  {url_plan} : {stats}")
                rapport["sources"].append({"source": source["nom"], "page": url_plan, **stats})
                for cle in lues:
                    marques_vues[cle] = {"detecte_le": maintenant}
                for f in fiches:
                    if f["id"] in ids_vus:
                        continue
                    ids_vus.add(f["id"])
                    f.update({"source": source["nom"], "promoteur": "à identifier sur la fiche",
                              "contacts": [], "detecte_le": maintenant, "origine": "catalogue"})
                    ajouts.append(f)
            continue

        for url_page in source["pages"]:
            html = telecharger(url_page)
            if not html:
                print(f"  page non lue : {url_page}")
                rapport["sources"].append({"source": source["nom"], "page": url_page, "non_lue": True})
                continue
            if fmt == "liste":
                trouves, stats = trouver_listing(url_page, html)
            else:
                trouves, stats = trouver_promoteur(url_page, html, mots)
            print(f"  {url_page} : {len(html)} caractères, {stats}")
            rapport["sources"].append({"source": source["nom"], "page": url_page, **stats})

            for item in trouves:
                if item["id"] in seen or item["id"] in ids_vus:
                    continue
                ids_vus.add(item["id"])
                item["source"] = source["nom"]
                item["detecte_le"] = maintenant
                if fmt == "promoteur":
                    item["promoteur"] = source["nom"]
                    item["contacts"] = chercher_contacts(item["lien"], cache_contacts)
                else:
                    item["promoteur"] = "à identifier sur la fiche"
                    item["contacts"] = []
                # Le premier passage sert de référence : pas d'alerte pour ce qui est déjà en ligne.
                item["origine"] = "catalogue" if premier_passage else "alerte"
                ajouts.append(item)

    for cle, valeur in marques_vues.items():
        seen.setdefault(cle, valeur)
    for f in ajouts:
        seen[f["id"]] = {"detecte_le": f["detecte_le"]}
        if f["origine"] == "alerte":
            f["alerte_envoyee"] = False
        programmes.append(f)

    # Alertes pas encore envoyées : les nouvelles et celles d'un passage précédent dont le mail a échoué.
    en_attente = [p for p in programmes if p.get("origine") == "alerte" and not p.get("alerte_envoyee")]
    print(f"{len(ajouts)} nouvelle(s) fiche(s) enregistrée(s), {len(en_attente)} alerte(s) à envoyer.")
    echec_mail = False
    if en_attente:
        sujet = f"[Veille] {len(en_attente)} nouveau(x) programme(s) en Île-de-France"
        html, texte = construire_mail(en_attente)
        if send_mail(sujet, html, texte):
            for p in en_attente:
                p["alerte_envoyee"] = True
        else:
            # Les données sont tout de même enregistrées ; le mail sera retenté au passage suivant.
            echec_mail = True

    rapport["nouvelles_fiches"] = len(ajouts)
    rapport["alertes_en_attente"] = len(en_attente)
    rapport["echec_mail"] = echec_mail
    save_json(os.path.join(DATA_DIR, "rapport.json"), rapport)
    save_json(os.path.join(DATA_DIR, "seen.json"), seen)
    save_json(os.path.join(DATA_DIR, "programmes.json"), programmes)
    save_json(os.path.join(DOCS_DIR, "programmes.json"), programmes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
