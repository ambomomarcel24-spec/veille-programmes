#!/usr/bin/env python3
"""Veille des programmes immobiliers neufs en Île-de-France.

Sources (champ « format » dans config/sources.json) :
- « liste » : pages de résultats par département (SeLoger Neuf). Fiches avec département et mention.
- « catalogue » : plan du site listant des fiches (Trouver un logement neuf). Les fiches situées en IDF
  sont gardées ; 150 fiches lues au maximum par passage.
- « promoteur » : site d'un promoteur, filtré par mots-clés.

Pour chaque programme enregistré :
- le statut est déduit du texte (nouveau, avant-première, exclusivité, nouvelle opportunité,
  lancement commercial, ou autre) ;
- la fiche est lue pour retrouver le nom du promoteur ;
- le site du promoteur est pris dans config/promoteurs_connus.json, ou sur la fiche si elle le donne ;
- le mail générique est cherché sur le site du promoteur quand il est connu.

Tout promoteur rencontré est référencé dans data/promoteurs.json (publié dans docs/).

Règles d'alerte : seules les fiches « liste » découvertes APRÈS le premier passage déclenchent un mail.
"""
import hashlib
import html as html_lib
import os
import re
import sys
import time
import unicodedata
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from common import CONFIG_DIR, DATA_DIR, DOCS_DIR, load_json, now_iso, save_json, send_mail

USER_AGENT = "VeilleProgrammesFFY/1.0 (usage interne ; marcel.ambomo@flyingforyou.fr)"
TIMEOUT = 20
DELAY_SECONDES = 2
MAX_FICHES_CATALOGUE = 150      # fiches lues par passage sur le plan du site
MAX_ENRICHISSEMENTS = 60        # fiches relues par passage pour trouver le promoteur

DEPARTEMENTS_IDF = ("75", "77", "78", "91", "92", "93", "94", "95")
LOC_IDF = re.compile(r"\b(?:75|77|78|91|92|93|94|95)\d{3}\b|\((?:75|77|78|91|92|93|94|95)\)")
RE_FICHE_LISTE = re.compile(r"/programme/([a-z0-9-]+)-(\d{2})/(\d+)/?")

STATUTS = [
    ("avant-première", ["avant-première", "avant première"]),
    ("exclusivité", ["exclusivité", "exclusivite"]),
    ("nouvelle opportunité", ["nouvelle opportunité", "offre spéciale", "retour de lots", "opportunité"]),
    ("lancement commercial", ["lancement commercial", "lancement exceptionnel"]),
    ("nouveau", ["nouveauté", "nouveau programme"]),
]
STATUT_AUTRE = "autre"

PROMOTEUR_LIBELLES = re.compile(
    r"(?:Proposé par|Vendu par|Commercialisé par|Promoteur\s*:|vous est proposé par|COORDONNÉES DU VENDEUR\s*:?|"
    r"Programme (?:réalisé|proposé) par|Réalisé par)\s*:?\s*",
    re.IGNORECASE,
)
RESEAUX = ("facebook", "instagram", "linkedin", "twitter", "youtube", "tiktok", "google", "x.com",
           "wikipedia", "georisques", "seloger", "bellesdemeures", "trouver-un-logement", "pagesjaunes")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PREFIXES_GENERIQUES = ("contact", "commercial", "info", "accueil", "reservation", "reservations", "hello")
PAGES_CONTACT = ("/contact", "/contactez-nous", "/mentions-legales", "/nous-contacter")
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


# --- Outils --------------------------------------------------------------------------------

def normaliser(texte):
    return re.sub(r"\s+", " ", texte or "").strip()


def contient(texte, mots):
    t = texte.lower()
    return any(m.lower() in t for m in mots)


def identifiant(*morceaux):
    return hashlib.sha1("|".join(morceaux).encode("utf-8")).hexdigest()[:16]


def slug(texte):
    """« SEQENS ACCESSION » -> « seqens accession » (sans accents), pour comparer les noms."""
    t = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def nom_ville(slug_ville):
    mots = slug_ville.split("-")
    return " ".join(m if (i and m in PETITS_MOTS) else m.capitalize() for i, m in enumerate(mots))


def detecter_statut(texte):
    t = (texte or "").lower()
    for statut, mots in STATUTS:
        if any(m in t for m in mots):
            return statut
    return STATUT_AUTRE


def mois_en_nombre(token):
    t = token.lower().rstrip(".")
    for prefixe, numero in MOIS_PREFIXES:
        if t.startswith(prefixe):
            return numero
    return None


def extraire_date(texte):
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


# --- Lecture des sources -------------------------------------------------------------------

def texte_de_la_carte(lien, repli):
    """Remonte depuis le lien jusqu'au plus grand bloc ne contenant qu'une seule fiche."""
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
    soup = BeautifulSoup(html, "html.parser")
    fiches = {}
    stats = {"liens_programme": 0, "hors_idf": 0}
    for a in soup.select('a[href*="/programme/"]'):
        m = RE_FICHE_LISTE.search(a["href"])
        if not m:
            continue
        slug_ville, dept, numero = m.groups()
        if dept not in DEPARTEMENTS_IDF:
            stats["hors_idf"] += 1
            continue
        stats["liens_programme"] += 1
        fiche = fiches.setdefault(numero, {
            "slug": slug_ville, "dept": dept, "nom": "", "texte": "",
            "href": urljoin(url_page, a["href"]).split("?")[0],
        })
        lien_texte = normaliser(a.get_text(" "))
        if len(lien_texte) > len(fiche["nom"]):
            fiche["nom"] = lien_texte
        if not fiche["texte"]:
            fiche["texte"] = texte_de_la_carte(a, lien_texte)

    trouves = []
    for numero, f in fiches.items():
        nom = f["nom"] or f"Programme {nom_ville(f['slug'])}"
        mention = next((m for m in ("Nouveauté", "Lancement commercial", "Lancement exceptionnel", "Offre spéciale",
                                    "Emménagement immédiat", "Retour de lots", "Travaux en cours",
                                    "Avant-première", "Exclusivité")
                        if m.lower() in f["texte"].lower()), "")
        trouves.append({
            "id": identifiant("selogerneuf", numero),
            "nom": nom,
            "lieu": f"{nom_ville(f['slug'])} ({f['dept']})",
            "lien": f["href"],
            "mention": mention,
            "statut": detecter_statut(f["texte"]),
            "date_publication": None,
            "extrait": f["texte"][:300],
        })
    stats["idf"] = len(trouves)
    return trouves, stats


def trouver_promoteur(url_page, html, mots):
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
            "statut": detecter_statut(texte),
            "date_publication": extraire_date(texte),
            "extrait": texte[:300],
        })
    return trouves, stats


def trouver_catalogue(url_sitemap, deja_connus):
    """Lit les fiches non connues du plan du site ; retourne (fiches_idf, cles_lues, stats)."""
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
            continue
        stats["lues"] += 1
        lues.append(cle)
        soup = BeautifulSoup(page, "html.parser")
        h1 = soup.find("h1")
        titre = normaliser(h1.get_text(" ")) if h1 else ""
        meta = soup.find("meta", attrs={"name": "description"})
        description = normaliser(meta.get("content", "")) if meta else ""
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
            "statut": detecter_statut(f"{titre} {description}"),
            "date_publication": None,
            "extrait": titre[:300],
        })
    return idf, lues, stats


# --- Promoteurs ----------------------------------------------------------------------------

def extraire_promoteur(soup, texte):
    """Nom du promoteur ou vendeur, d'après les libellés habituels de la fiche."""
    for m in PROMOTEUR_LIBELLES.finditer(texte):
        apres = texte[m.end():m.end() + 80]
        candidat = re.split(r"\s*(?:,|\.\s|·|\||\(|mis à jour|\bLe programme\b|\n)", apres)[0].strip(" :-")
        candidat = re.sub(r"^(?:proposé par|vendu par|commercialisé par|réalisé par|promoteur)\s*:?\s*", "",
                          candidat, flags=re.IGNORECASE).strip(" :-")
        if 3 <= len(candidat) <= 60 and not re.search(r"\d{5}|@|http", candidat):
            return candidat
    return None


def site_sur_la_fiche(soup, portail_domaine, nom_promoteur):
    """Lien externe vers le site du promoteur, seulement s'il porte le nom du promoteur."""
    jetons = [j for j in slug(nom_promoteur).split() if len(j) >= 4]
    if not jetons:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        domaine = urlparse(href).netloc.lower().removeprefix("www.")
        if not domaine or portail_domaine in domaine or any(r in domaine for r in RESEAUX):
            continue
        if any(j in slug(domaine) for j in jetons):
            return f"{urlparse(href).scheme}://{urlparse(href).netloc}"
    return None


def chercher_contacts_site(site, cache):
    """Adresses génériques (contact@, commercial@…) trouvées sur le site du promoteur."""
    if site in cache:
        return cache[site]
    p = urlparse(site)
    domaine = p.netloc.removeprefix("www.")
    trouves = []
    for url in [site + "/"] + [site + c for c in PAGES_CONTACT]:
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


def cle_promoteur(nom):
    return slug(nom).replace(" ", "-")


def enregistrer_promoteur(registre, nom, site=None, emails=None, programme=None, source=None):
    """Ajoute ou met à jour un promoteur dans le registre."""
    if not nom:
        return
    cle = cle_promoteur(nom)
    p = registre.setdefault(cle, {"nom": nom, "site": None, "emails": [], "programmes": [], "sources": [], "maj": None})
    if site and not p["site"]:
        p["site"] = site
    for e in emails or []:
        if e not in p["emails"]:
            p["emails"].append(e)
    if programme:
        ref = {"nom": programme["nom"], "lieu": programme["lieu"], "lien": programme["lien"],
               "statut": programme.get("statut", STATUT_AUTRE)}
        if not any(x["lien"] == ref["lien"] for x in p["programmes"]):
            p["programmes"].append(ref)
    if source and source not in p["sources"]:
        p["sources"].append(source)
    p["maj"] = now_iso()


def enrichir(item, registre, cache_contacts, portail_domaine):
    """Relit la fiche pour trouver le promoteur, son site et son mail générique."""
    html = telecharger(item["lien"])
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    texte = normaliser(soup.get_text(" "))
    nom = extraire_promoteur(soup, texte)
    cle = cle_promoteur(nom) if nom else None
    site = None
    if nom:
        site = registre.get(cle, {}).get("site") or site_sur_la_fiche(soup, portail_domaine, nom)
        if not site:
            site = site_connu(nom)
        item["promoteur"] = nom
        item["promoteur_site"] = site
        item["contacts"] = chercher_contacts_site(site, cache_contacts) if site else []
        enregistrer_promoteur(registre, nom, site=site, emails=item["contacts"], programme=item,
                              source=item.get("source"))
    item["enrichi"] = True
    return True


def site_connu(nom):
    """Site renseigné à la main dans config/promoteurs_connus.json."""
    for p in load_json(os.path.join(CONFIG_DIR, "promoteurs_connus.json"), []):
        if cle_promoteur(p["nom"]) == cle_promoteur(nom) and p.get("site"):
            return p["site"]
    return None


# --- Mails ---------------------------------------------------------------------------------

def construire_mail(nouveaux):
    lignes_html, lignes_txt = [], []
    for i in nouveaux:
        contacts = ", ".join(i.get("contacts", [])) or "non trouvé : voir la fiche"
        lignes_html.append(
            "<li>"
            f"<b>{html_lib.escape(i['nom'])}</b><br>"
            f"Statut : {html_lib.escape(i.get('statut', STATUT_AUTRE))}<br>"
            f"Lieu : {html_lib.escape(i['lieu'])}<br>"
            f"Promoteur : {html_lib.escape(i.get('promoteur', 'à identifier'))}<br>"
            f"Site : {html_lib.escape(i.get('promoteur_site') or 'non trouvé')}<br>"
            f"Contact : {html_lib.escape(contacts)}<br>"
            f"Source : <a href='{html_lib.escape(i['lien'])}'>{html_lib.escape(i['lien'])}</a>"
            "</li>"
        )
        lignes_txt.append(
            f"- {i['nom']}\n  Statut : {i.get('statut', STATUT_AUTRE)}\n  Lieu : {i['lieu']}\n"
            f"  Promoteur : {i.get('promoteur', 'à identifier')}\n"
            f"  Site : {i.get('promoteur_site') or 'non trouvé'}\n"
            f"  Contact : {contacts}\n  Source : {i['lien']}"
        )
    html = (
        f"<p>{len(nouveaux)} nouveau(x) programme(s) repéré(s) en Île-de-France :</p>"
        f"<ul>{''.join(lignes_html)}</ul>"
    )
    texte = f"{len(nouveaux)} nouveau(x) programme(s) repéré(s) :\n\n" + "\n\n".join(lignes_txt)
    return html, texte


# --- Passage principal ---------------------------------------------------------------------

def fusionner_promoteurs_connus(registre):
    for p in load_json(os.path.join(CONFIG_DIR, "promoteurs_connus.json"), []):
        enregistrer_promoteur(registre, p["nom"], site=p.get("site"), emails=p.get("emails", []))


def main():
    sources = load_json(os.path.join(CONFIG_DIR, "sources.json"), [])
    mots = load_json(os.path.join(CONFIG_DIR, "keywords.json"), {})
    seen = load_json(os.path.join(DATA_DIR, "seen.json"), {})
    programmes = load_json(os.path.join(DATA_DIR, "programmes.json"), [])
    registre = load_json(os.path.join(DATA_DIR, "promoteurs.json"), {})
    fusionner_promoteurs_connus(registre)
    for p in programmes:  # fiches enregistrées avant l'ajout des statuts
        if "statut" not in p:
            p["statut"] = detecter_statut(p.get("mention", ""))

    premier_passage = not seen
    if premier_passage:
        print("Premier passage : enregistrement de la référence initiale, sans envoi de mail.")

    maintenant = now_iso()
    rapport = {"date": maintenant, "premier_passage": premier_passage, "sources": []}
    ajouts, ids_vus, marques_vues = [], set(), {}
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
                    f.update({"source": source["nom"], "promoteur": "à identifier", "contacts": [],
                              "detecte_le": maintenant, "origine": "catalogue", "enrichi": False})
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
                item["contacts"] = []
                item["enrichi"] = False
                if fmt == "promoteur":
                    item["promoteur"] = source["nom"]
                    item["enrichi"] = True
                    site_source = f"https://{urlparse(url_page).netloc}"
                    enregistrer_promoteur(registre, source["nom"], site=site_source,
                                          source=source["nom"], programme=item)
                    item["contacts"] = chercher_contacts_site(site_source, cache_contacts)
                else:
                    item["promoteur"] = "à identifier"
                item["origine"] = "catalogue" if premier_passage else "alerte"
                ajouts.append(item)

    # Promoteur, site et mail générique des fiches pas encore relues (nouvelles d'abord).
    a_relire = [p for p in programmes + ajouts if not p.get("enrichi")]
    a_relire.sort(key=lambda p: (p.get("origine") != "alerte", p.get("detecte_le", "")), reverse=False)
    relues = 0
    for item in a_relire:
        if relues >= MAX_ENRICHISSEMENTS:
            break
        domaine = urlparse(item["lien"]).netloc.removeprefix("www.")
        if enrichir(item, registre, cache_contacts, domaine):
            relues += 1
    rapport["fiches_relues"] = relues
    rapport["fiches_a_relire"] = len([p for p in programmes + ajouts if not p.get("enrichi")])

    for cle, valeur in marques_vues.items():
        seen.setdefault(cle, valeur)
    for f in ajouts:
        seen[f["id"]] = {"detecte_le": f["detecte_le"]}
        if f["origine"] == "alerte":
            f["alerte_envoyee"] = False
        programmes.append(f)

    en_attente = [p for p in programmes if p.get("origine") == "alerte" and not p.get("alerte_envoyee")]
    print(f"{len(ajouts)} nouvelle(s) fiche(s), {relues} fiche(s) relue(s), {len(en_attente)} alerte(s) à envoyer.")
    echec_mail = False
    if en_attente:
        sujet = f"[Veille] {len(en_attente)} nouveau(x) programme(s) en Île-de-France"
        html, texte = construire_mail(en_attente)
        if send_mail(sujet, html, texte):
            for p in en_attente:
                p["alerte_envoyee"] = True
        else:
            echec_mail = True

    rapport["nouvelles_fiches"] = len(ajouts)
    rapport["alertes_en_attente"] = len(en_attente)
    rapport["echec_mail"] = echec_mail
    save_json(os.path.join(DATA_DIR, "rapport.json"), rapport)
    save_json(os.path.join(DATA_DIR, "seen.json"), seen)
    save_json(os.path.join(DATA_DIR, "programmes.json"), programmes)
    save_json(os.path.join(DATA_DIR, "promoteurs.json"), registre)
    save_json(os.path.join(DOCS_DIR, "programmes.json"), programmes)
    save_json(os.path.join(DOCS_DIR, "promoteurs.json"), registre)
    return 0


if __name__ == "__main__":
    sys.exit(main())
