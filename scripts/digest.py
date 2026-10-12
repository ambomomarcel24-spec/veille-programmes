#!/usr/bin/env python3
"""Récapitulatif hebdomadaire : envoie la liste des programmes détectés dans les 7 derniers jours.

Le workflow tourne deux fois le vendredi (13h et 14h UTC) pour couvrir l'heure d'été et d'hiver ;
ce script n'envoie quelque chose que lorsqu'il est vendredi 15h à Paris.
"""
import html as html_lib
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common import DATA_DIR, load_json, send_mail


def main(forcer=False):
    maintenant_paris = datetime.now(ZoneInfo("Europe/Paris"))
    if not forcer and not (maintenant_paris.weekday() == 4 and maintenant_paris.hour == 15):
        print(f"Pas l'heure du récapitulatif ({maintenant_paris:%A %H:%M} à Paris).")
        return 0

    programmes = load_json(os.path.join(DATA_DIR, "programmes.json"), [])
    limite = datetime.now(timezone.utc) - timedelta(days=7)
    semaine = [p for p in programmes if datetime.fromisoformat(p["detecte_le"]) >= limite]
    semaine.sort(key=lambda p: p["detecte_le"], reverse=True)

    if not semaine:
        print("Aucun programme détecté cette semaine : pas de récapitulatif envoyé.")
        return 0

    lignes_html = []
    lignes_txt = []
    for p in semaine:
        contacts = ", ".join(p.get("contacts", [])) or "non trouvé : voir la fiche"
        lignes_html.append(
            "<li>"
            f"<b>{html_lib.escape(p['nom'])}</b><br>"
            f"Lieu : {html_lib.escape(p['lieu'])}<br>"
            f"Promoteur : {html_lib.escape(p['promoteur'])}<br>"
            f"Contact : {html_lib.escape(contacts)}<br>"
            f"Détecté le : {html_lib.escape(p['detecte_le'])}<br>"
            f"Source : <a href='{html_lib.escape(p['lien'])}'>{html_lib.escape(p['lien'])}</a>"
            "</li>"
        )
        lignes_txt.append(
            f"- {p['nom']}\n  Lieu : {p['lieu']}\n  Promoteur : {p['promoteur']}\n"
            f"  Contact : {contacts}\n  Détecté le : {p['detecte_le']}\n  Source : {p['lien']}"
        )

    sujet = f"[Veille] Récapitulatif de la semaine : {len(semaine)} programme(s)"
    html = (
        f"<p>Programmes repérés en Île-de-France cette semaine ({len(semaine)}) :</p>"
        f"<ul>{''.join(lignes_html)}</ul>"
    )
    texte = f"Programmes repérés cette semaine ({len(semaine)}) :\n\n" + "\n\n".join(lignes_txt)

    if not send_mail(sujet, html, texte):
        return 1
    print(f"Récapitulatif envoyé : {len(semaine)} programme(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(forcer="--forcer" in sys.argv))
