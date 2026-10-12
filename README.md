# Veille programmes immobiliers neufs (Île-de-France)

Surveille les sites de promoteurs et de portails du neuf, repère les nouveaux programmes
(« nouveauté », « avant-première », « exclusivité »…), et envoie :

- **un mail dès qu'un nouveau programme est détecté** (au plus une fois par heure, et seulement s'il y a du nouveau) ;
- **un récapitulatif chaque vendredi à 15h** (heure de Paris) avec tous les programmes détectés dans la semaine.

Une page web listant les programmes est publiée via GitHub Pages.

## Fonctionnement

| Fichier | Rôle |
|---|---|
| `config/sources.json` | Liste des promoteurs et portails surveillés (ajoutez-y un site en copiant un bloc). |
| `config/keywords.json` | Mots-clés de détection (déclencheurs et mots liés aux programmes). |
| `scripts/veille.py` | Parcourt les sites, détecte les nouveautés, envoie l'alerte. Tourne toutes les heures. |
| `scripts/digest.py` | Envoie le récapitulatif du vendredi à 15h (heure de Paris). |
| `data/programmes.json` | Historique des programmes détectés. |
| `data/seen.json` | Identifiants déjà traités, pour ne jamais envoyer deux fois la même alerte. |
| `docs/` | Page web (GitHub Pages). |

Les deux automatisations sont dans `.github/workflows/`. Elles tournent sur les serveurs de GitHub :
votre ordinateur n'a rien à faire.

## Mise en place (une seule fois)

### 1. Créer la clé d'envoi Brevo
1. Créez un compte gratuit sur brevo.com.
2. Dans **Senders & IP** (ou **Expéditeurs**), ajoutez une adresse expéditrice et validez-la depuis le mail de confirmation.
3. Dans **SMTP & API → API Keys**, créez une clé et copiez-la.

### 2. Enregistrer les secrets dans GitHub
Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**. Créez ces trois secrets :

| Nom | Valeur |
|---|---|
| `BREVO_API_KEY` | la clé API Brevo |
| `MAIL_TO` | `marcel.ambomo@flyingforyou.fr` |
| `MAIL_FROM` | l'adresse expéditrice validée dans Brevo |

Ne mettez jamais ces valeurs dans le code ou dans une conversation.

### 3. Activer la page web
Dans **Settings → Pages** : *Source* = *Deploy from a branch*, branche `main`, dossier `/docs`.
La page sera disponible à `https://ambomomarcel24-spec.github.io/veille-programmes/`.

### 4. Vérifier que tout fonctionne
Onglet **Actions** → « Veille programmes » → **Run workflow**. Au premier lancement, le système enregistre
les programmes déjà en ligne **sans envoyer de mail** (sinon vous recevriez des dizaines d'anciennes annonces).
À partir du deuxième passage, seules les nouveautés déclenchent un mail.

## Limites à connaître

- **Les sites changent de structure.** Les pages listées dans `sources.json` sont des suppositions à valider au premier lancement :
  si une page ne donne rien, ajustez son adresse ou retirez-la. Les logs de l'onglet Actions indiquent les erreurs.
- **Les sites très dynamiques** (chargés par JavaScript) ne livrent pas leur contenu au robot. Ils seront à suivre à la main.
- **Les contacts** sont les adresses génériques (contact@, commercial@, info@…) trouvées sur le site du programme ou sur ses pages contact et mentions légales.
  Pour un programme repéré sur un portail, le contact peut manquer et doit être trouvé sur la fiche.
- **Le robot respecte** `robots.txt` et attend au moins 2 secondes entre deux requêtes vers un même site.
- **La détection n'est pas parfaite** : elle peut manquer une annonce ou en signaler une qui n'est pas un nouveau programme. Un contrôle humain reste nécessaire.
- **GitHub met en pause les automatisations** après 60 jours sans activité sur le dépôt. Si les alertes s'arrêtent, relancez le workflow depuis l'onglet Actions.
