"""
Application d'Analyse Statistiques NHL (via api-web.nhle.com)
===================================================================
Même structure que MLB / NPB / KBO :
  - Résumé du jour
  - Hot Pronostics (buteurs, passeurs, totaux buts, écarts de points)
  - Analyse par Équipe
  - Prédictions du jour

Correspondances baseball → hockey :
  - Home Runs  → Buteurs (buts)
  - Runs       → Passeurs (passes décisives)
  - ERA/WHIP   → GAA / % arrêts du gardien
"""

import streamlit as st
import pandas as pd
import altair as alt
import time
import json
import os
import requests
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path as _Path
import importlib.util as _importlib_util
import sys as _sys

# ---------------------------------------------------------------------------
# Design system partagé
# ---------------------------------------------------------------------------
_THEME_PATH = next(
    (
        p
        for p in (
            _Path(__file__).resolve().parent / "shared" / "theme.py",
            _Path(__file__).resolve().parent.parent / "shared" / "theme.py",
        )
        if p.is_file()
    ),
    None,
)
if _THEME_PATH is None:
    raise ImportError("shared/theme.py introuvable à côté de l'app NHL.")
_spec = _importlib_util.spec_from_file_location("ps_shared_theme", _THEME_PATH)
_ps_theme = _importlib_util.module_from_spec(_spec)
_sys.modules["ps_shared_theme"] = _ps_theme
_spec.loader.exec_module(_ps_theme)
apply_theme = _ps_theme.apply_theme
render_page_header = _ps_theme.render_page_header
render_section_title = _ps_theme.render_section_title
afficher_cartes_matchs = _ps_theme.afficher_cartes_matchs
afficher_badge_value_bet = _ps_theme.afficher_badge_value_bet
afficher_tableau_recap_hot_pronostics = _ps_theme.afficher_tableau_recap_hot_pronostics
afficher_outil_coherence_totaux = _ps_theme.afficher_outil_coherence_totaux
render_footer = _ps_theme.render_footer
render_prediction_match_banner = _ps_theme.render_prediction_match_banner

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
NHL_API = "https://api-web.nhle.com/v1"
TZ_EASTERN = ZoneInfo("America/New_York")
TZ_PARIS = ZoneInfo("Europe/Paris")
ODDS_API_SPORT_KEY = "icehockey_nhl"
LIGUE_PAR_DEFAUT = "NHL"
SEUILS_PARIS_PAR_LIGUE = {
    "NHL": {
        "gaa_mauvais": 3.20,
        "gaa_excellent": 2.40,
        "buts_total_haut": 6.0,
    },
}
NOM_FICHIER_HISTORIQUE = "historique_predictions_nhl.json"
CHEMIN_HISTORIQUE = os.path.join(os.path.dirname(os.path.abspath(__file__)), NOM_FICHIER_HISTORIQUE)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "PARIS-SPORTIFS-NHL-Stats-App/1.0",
    "Accept": "application/json",
})


def appeler_avec_retry(fonction, *args, tentatives: int = 3, delai_base: float = 0.5, **kwargs):
    derniere = None
    for tentative in range(1, tentatives + 1):
        try:
            return fonction(*args, **kwargs)
        except Exception as exc:
            derniere = exc
            if tentative < tentatives:
                time.sleep(delai_base * (2 ** (tentative - 1)))
    raise derniere


def _get_json(url: str, timeout: int = 20):
    reponse = _SESSION.get(url, timeout=timeout)
    reponse.raise_for_status()
    return reponse.json()


def _texte_localise(valeur) -> str:
    if isinstance(valeur, dict):
        return str(valeur.get("default") or valeur.get("fr") or next(iter(valeur.values()), "") or "")
    return str(valeur or "")


def _parser_float(valeur, defaut=0.0) -> float:
    try:
        if valeur is None or (isinstance(valeur, float) and pd.isna(valeur)):
            return float(defaut)
        return float(valeur)
    except (TypeError, ValueError):
        return float(defaut)


def _normaliser_nom(texte: str) -> str:
    if not texte:
        return ""
    texte = unicodedata.normalize("NFKD", str(texte))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return "".join(ch.lower() for ch in texte if ch.isalnum())


st.set_page_config(
    page_title="Analyse NHL - Buteurs & Passeurs",
    page_icon="🏒",
    layout="wide",
)
apply_theme("nhl")

# ---------------------------------------------------------------------------
# Secrets / historique
# ---------------------------------------------------------------------------
def _lire_conf_github():
    try:
        conf = st.secrets.get("github", {})
        token = conf.get("token")
        gist_id = conf.get("gist_id")
        if token and gist_id:
            return token, gist_id
    except Exception:
        pass
    return None, None


def _lire_cle_odds_api():
    try:
        conf = st.secrets.get("odds_api", {})
        cle = conf.get("api_key")
        return cle if cle else None
    except Exception:
        return None


def _charger_historique_predictions() -> dict:
    token, gist_id = _lire_conf_github()
    if token and gist_id:
        try:
            reponse = requests.get(
                f"https://api.github.com/gists/{gist_id}",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                timeout=10,
            )
            if reponse.ok:
                fichier = reponse.json().get("files", {}).get(NOM_FICHIER_HISTORIQUE)
                if fichier and fichier.get("content"):
                    return json.loads(fichier["content"])
        except Exception:
            pass
    if os.path.exists(CHEMIN_HISTORIQUE):
        try:
            with open(CHEMIN_HISTORIQUE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _sauvegarder_predictions_du_jour(date_str: str, matches_snapshot: list) -> None:
    historique = _charger_historique_predictions()
    historique[date_str] = {"matches": matches_snapshot, "saved_at": datetime.now(TZ_PARIS).isoformat()}
    contenu_json = json.dumps(historique, ensure_ascii=False, indent=2)
    token, gist_id = _lire_conf_github()
    if token and gist_id:
        try:
            requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                json={"files": {NOM_FICHIER_HISTORIQUE: {"content": contenu_json}}},
                timeout=10,
            )
            return
        except Exception:
            pass
    try:
        with open(CHEMIN_HISTORIQUE, "w", encoding="utf-8") as f:
            f.write(contenu_json)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Données NHL
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def obtenir_standings_nhl():
    data = appeler_avec_retry(_get_json, f"{NHL_API}/standings/now")
    lignes = []
    for row in data.get("standings") or []:
        abbr = _texte_localise(row.get("teamAbbrev")).upper()
        if not abbr:
            continue
        gp = max(1, int(row.get("gamesPlayed") or 1))
        lignes.append({
            "abbr": abbr,
            "nom": _texte_localise(row.get("teamName")) or abbr,
            "common": _texte_localise(row.get("teamCommonName")) or abbr,
            "conference": row.get("conferenceName") or "",
            "division": row.get("divisionName") or "",
            "gp": int(row.get("gamesPlayed") or 0),
            "w": int(row.get("wins") or 0),
            "l": int(row.get("losses") or 0),
            "otl": int(row.get("otLosses") or 0),
            "pts": int(row.get("points") or 0),
            "gf": int(row.get("goalFor") or 0),
            "ga": int(row.get("goalAgainst") or 0),
            "gf_pg": _parser_float(row.get("goalFor")) / gp,
            "ga_pg": _parser_float(row.get("goalAgainst")) / gp,
            "l10_gf": _parser_float(row.get("l10GoalsFor")),
            "l10_ga": _parser_float(row.get("l10GoalsAgainst")),
            "l10_gp": max(1, int(row.get("l10GamesPlayed") or 1)),
            "l10_w": int(row.get("l10Wins") or 0),
            "season_id": row.get("seasonId"),
            "logo": row.get("teamLogo"),
        })
    return pd.DataFrame(lignes)


@st.cache_data(show_spinner=False, ttl=3600)
def get_teams_nhl_dict():
    df = obtenir_standings_nhl()
    if df.empty:
        return {}
    return dict(zip(df["abbr"], df["nom"]))


def saison_stats_courante() -> int:
    df = obtenir_standings_nhl()
    if df.empty or df["season_id"].isna().all():
        # Repli : saison NHL typique (année d'automne)*10000 + année de printemps
        now = datetime.now(TZ_EASTERN)
        debut = now.year if now.month >= 9 else now.year - 1
        return debut * 10000 + (debut + 1)
    return int(df["season_id"].dropna().iloc[0])


@st.cache_data(show_spinner=False, ttl=300)
def obtenir_scores_du_jour(date_str: str = None):
    """Scores / calendrier du jour (heure de l'Est). Si vide, prochains matchs de la semaine."""
    date_ref = date_str or datetime.now(TZ_EASTERN).strftime("%Y-%m-%d")
    data = appeler_avec_retry(_get_json, f"{NHL_API}/score/{date_ref}")
    games = data.get("games") or []
    source = "jour"
    if not games:
        sched = appeler_avec_retry(_get_json, f"{NHL_API}/schedule/now")
        for day in sched.get("gameWeek") or []:
            day_games = day.get("games") or []
            if day_games:
                # normaliser vers format score-like
                games = day_games
                date_ref = day.get("date") or date_ref
                source = "prochains"
                break
    return games, date_ref, source


def _heure_paris_depuis_utc(start_utc: str):
    if not start_utc:
        return "—", "—"
    try:
        dt_utc = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        return (
            dt_utc.astimezone(TZ_EASTERN).strftime("%H:%M %Z"),
            dt_utc.astimezone(TZ_PARIS).strftime("%d/%m à %H:%M"),
        )
    except Exception:
        return "—", "—"


def _statut_match(game_state: str) -> str:
    s = (game_state or "").upper()
    if s in ("OFF", "FINAL", "FINAL/OT", "FINAL/SO"):
        return "Terminé"
    if s in ("LIVE", "CRIT"):
        return "En cours"
    if s in ("FUT", "PRE"):
        return "À venir"
    return game_state or "—"


@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_club_stats(abbr: str):
    abbr = (abbr or "").upper()
    if not abbr:
        return {"skaters": [], "goalies": [], "season": None}
    data = appeler_avec_retry(_get_json, f"{NHL_API}/club-stats/{abbr}/now")
    return {
        "skaters": data.get("skaters") or [],
        "goalies": data.get("goalies") or [],
        "season": data.get("season"),
        "gameType": data.get("gameType"),
    }


@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_meilleur_gardien(abbr: str):
    stats = obtenir_club_stats(abbr)
    goalies = [g for g in stats.get("goalies") or [] if int(g.get("gamesStarted") or g.get("gamesPlayed") or 0) > 0]
    if not goalies:
        return None
    # Priorité : plus de départs, puis meilleur GAA
    goalies = sorted(
        goalies,
        key=lambda g: (-int(g.get("gamesStarted") or 0), _parser_float(g.get("goalsAgainstAverage"), 99)),
    )
    g = goalies[0]
    return {
        "id": g.get("playerId"),
        "nom": f"{_texte_localise(g.get('firstName'))} {_texte_localise(g.get('lastName'))}".strip(),
        "gaa": _parser_float(g.get("goalsAgainstAverage"), 3.0),
        "sv_pct": _parser_float(g.get("savePercentage"), 0.900),
        "wins": int(g.get("wins") or 0),
        "gp": int(g.get("gamesPlayed") or 0),
        "starts": int(g.get("gamesStarted") or 0),
    }


@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_forme_equipe(abbr: str) -> dict:
    """Moyennes buts pour/contre : L10 standings si dispo, sinon saison."""
    df = obtenir_standings_nhl()
    row = df[df["abbr"] == (abbr or "").upper()]
    if row.empty:
        return {"gf": None, "ga": None, "source": "indisponible"}
    r = row.iloc[0]
    if r["l10_gp"] >= 3 and r["l10_gf"] > 0:
        return {
            "gf": float(r["l10_gf"]) / float(r["l10_gp"]),
            "ga": float(r["l10_ga"]) / float(r["l10_gp"]),
            "source": "L10",
            "w": int(r["l10_w"]),
            "gp": int(r["l10_gp"]),
        }
    return {
        "gf": float(r["gf_pg"]),
        "ga": float(r["ga_pg"]),
        "source": "saison",
        "w": int(r["w"]),
        "gp": int(r["gp"]),
    }


@st.cache_data(show_spinner=False, ttl=1800, max_entries=400)
def obtenir_forme_joueur(player_id: int, n: int = 10) -> dict:
    if not player_id:
        return {"goals": 0, "assists": 0, "points": 0, "gp": 0}
    data = appeler_avec_retry(_get_json, f"{NHL_API}/player/{int(player_id)}/game-log/now")
    logs = data.get("gameLog") or []
    # API renvoie souvent du plus récent au plus ancien
    recent = logs[:n]
    goals = sum(int(x.get("goals") or 0) for x in recent)
    assists = sum(int(x.get("assists") or 0) for x in recent)
    points = sum(int(x.get("points") or 0) for x in recent)
    return {"goals": goals, "assists": assists, "points": points, "gp": len(recent)}


def _nom_skater(s: dict) -> str:
    return f"{_texte_localise(s.get('firstName'))} {_texte_localise(s.get('lastName'))}".strip()


@st.cache_data(show_spinner=False, ttl=1800)
def top_skaters_equipe(abbr: str, top_n: int = 12, avec_gamelog: bool = True) -> list:
    """
    Top skaters d'une équipe.
    `avec_gamelog=False` (Hot Pronostics multi-équipes) : utilise uniquement les
    stats de saison (rapide, 1 appel club-stats). `True` : enrichit avec les
    10 derniers matchs via game-log (onglet Analyse / Prédictions mono-équipe).
    """
    stats = obtenir_club_stats(abbr)
    skaters = list(stats.get("skaters") or [])
    skaters = sorted(skaters, key=lambda s: (-int(s.get("points") or 0), -int(s.get("goals") or 0)))
    out = []
    for s in skaters[:top_n]:
        pid = s.get("playerId")
        gp = max(1, int(s.get("gamesPlayed") or 1))
        goals_s = int(s.get("goals") or 0)
        assists_s = int(s.get("assists") or 0)
        points_s = int(s.get("points") or 0)
        if avec_gamelog:
            forme = obtenir_forme_joueur(pid, 10)
            goals_10, assists_10, points_10, gp_10 = (
                forme["goals"], forme["assists"], forme["points"], forme["gp"]
            )
        else:
            # Proxy "10 matchs" = rythme saisonnier × 10 (évite N appels réseau)
            goals_10 = round(goals_s / gp * 10, 1)
            assists_10 = round(assists_s / gp * 10, 1)
            points_10 = round(points_s / gp * 10, 1)
            gp_10 = 10
        out.append({
            "id": pid,
            "nom": _nom_skater(s),
            "poste": s.get("positionCode") or "",
            "equipe": (abbr or "").upper(),
            "goals_saison": goals_s,
            "assists_saison": assists_s,
            "points_saison": points_s,
            "gp_saison": int(s.get("gamesPlayed") or 0),
            "goals_10": goals_10,
            "assists_10": assists_10,
            "points_10": points_10,
            "gp_10": gp_10,
        })
    return out


# ---------------------------------------------------------------------------
# Modèles heuristiques (structure identique baseball, métriques hockey)
# ---------------------------------------------------------------------------
def predire_buts_match(moyenne_gf_equipe, moyenne_ga_equipe, stats_gardien_adverse):
    """Équivalent de predire_runs_match : buts équipe + proxy adverse."""
    if moyenne_gf_equipe is None:
        return None
    if stats_gardien_adverse is not None and stats_gardien_adverse.get("gaa", 0) > 0:
        gaa = stats_gardien_adverse["gaa"]
        sv = stats_gardien_adverse.get("sv_pct") or 0.900
        buts_equipe = (moyenne_gf_equipe * 0.55) + (gaa * 0.45)
        if sv <= 0.895:
            buts_equipe *= 1.10
        elif sv >= 0.920:
            buts_equipe *= 0.90
        confiance = "Élevée" if stats_gardien_adverse.get("starts", 0) >= 15 else "Moyenne"
    else:
        buts_equipe = moyenne_gf_equipe
        confiance = "Faible"
    buts_adverse = moyenne_ga_equipe if moyenne_ga_equipe is not None and pd.notna(moyenne_ga_equipe) else moyenne_gf_equipe
    total = buts_equipe + buts_adverse
    return {
        "buts_equipe": round(buts_equipe, 1),
        "total_match": round(total, 1),
        "confiance": confiance,
    }


def predire_probabilite_victoire(
    moyenne_gf_nous,
    moyenne_offense_adverse,
    stats_gardien_nous,
    stats_gardien_adverse,
    est_domicile: bool,
):
    """Même logique pondérée que baseball : gardiens 60% / offense 40% / domicile +3."""
    GAA_NEUTRE, SV_NEUTRE = 2.90, 0.905

    def _qualite_gardien(stats):
        if not stats or not stats.get("gaa"):
            gaa, sv = GAA_NEUTRE, SV_NEUTRE
        else:
            gaa = max(0.5, float(stats["gaa"]))
            sv = float(stats.get("sv_pct") or SV_NEUTRE)
        # Qualité ↑ si GAA ↓ et SV% ↑
        return (1.0 / gaa) * 0.7 + max(sv, 0.85) * 0.3

    q_nous = _qualite_gardien(stats_gardien_nous)
    q_adv = _qualite_gardien(stats_gardien_adverse)
    part_gardien_nous = q_nous / (q_nous + q_adv) if (q_nous + q_adv) else 0.5

    gf_nous = moyenne_gf_nous if moyenne_gf_nous is not None and pd.notna(moyenne_gf_nous) else 3.0
    gf_adv = moyenne_offense_adverse if moyenne_offense_adverse is not None and pd.notna(moyenne_offense_adverse) else 3.0
    part_off_nous = gf_nous / (gf_nous + gf_adv) if (gf_nous + gf_adv) else 0.5

    score = part_gardien_nous * 0.60 + part_off_nous * 0.40
    pct_nous = score * 100
    if est_domicile:
        pct_nous += 3
    else:
        pct_nous -= 3
    pct_nous = max(15.0, min(85.0, pct_nous))
    return round(pct_nous, 1), round(100 - pct_nous, 1)


def predire_joueurs_du_jour(top_skaters: list, stats_gardien_adverse, top_n: int = 3) -> list:
    facteur = 1.0
    if stats_gardien_adverse and stats_gardien_adverse.get("gaa"):
        gaa = stats_gardien_adverse["gaa"]
        sv = stats_gardien_adverse.get("sv_pct") or 0.900
        facteur += max(0, (gaa - 2.90)) * 0.12
        facteur += max(0, (0.905 - sv)) * 2.0
        facteur = max(0.7, min(facteur, 1.5))

    resultats = []
    for s in top_skaters:
        indice_brut = (s["goals_10"] * 18) + (s["assists_10"] * 10) + (s["points_saison"] * 0.15)
        indice = min(95, round(indice_brut * facteur))
        if indice <= 0:
            continue
        confiance = "Élevée" if indice >= 65 else ("Moyenne" if indice >= 35 else "Faible")
        resultats.append({
            "nom": s["nom"],
            "goals_10": s["goals_10"],
            "assists_10": s["assists_10"],
            "indice": indice,
            "confiance": confiance,
        })
    resultats = sorted(resultats, key=lambda x: x["indice"], reverse=True)
    return resultats[:top_n]


def generer_recommandation_pari(
    pct_nous, pct_adverse, stats_gardien_nous, stats_gardien_adverse,
    prediction_buts, joueurs_a_surveiller, ligue: str = "NHL",
):
    seuils = SEUILS_PARIS_PAR_LIGUE.get(ligue or "NHL", SEUILS_PARIS_PAR_LIGUE["NHL"])

    def _arrondir_au_demi(v: float) -> float:
        return round(v * 2) / 2

    conseils = []
    if pct_nous is not None and pct_adverse is not None:
        if abs(pct_nous - pct_adverse) < 10:
            conseils.append(
                "⚠️ Match serré (Haut Risque sur la victoire). Privilégiez un pari sur "
                "les buts / joueurs plutôt que sur le vainqueur."
            )
        else:
            favori = "notre équipe" if pct_nous > pct_adverse else "l'équipe adverse"
            conseils.append(
                f"✅ Écart de probabilité net en faveur de {favori} (Faible Risque sur la "
                "victoire). Un pari sur le vainqueur est ici plus fiable qu'un pari sur les totaux."
            )

    gaa_nous = stats_gardien_nous["gaa"] if stats_gardien_nous and stats_gardien_nous.get("gaa") else None
    gaa_adv = stats_gardien_adverse["gaa"] if stats_gardien_adverse and stats_gardien_adverse.get("gaa") else None
    deux = gaa_nous is not None and gaa_adv is not None
    deux_mauvais = deux and gaa_nous > seuils["gaa_mauvais"] and gaa_adv > seuils["gaa_mauvais"]
    deux_excellents = deux and gaa_nous < seuils["gaa_excellent"] and gaa_adv < seuils["gaa_excellent"]
    total = prediction_buts.get("total_match") if prediction_buts else None
    tendance_haute = total is not None and total > seuils["buts_total_haut"]

    if total is not None:
        if deux_mauvais or tendance_haute:
            ligne = _arrondir_au_demi(total - 0.5)
            conseils.append(f"📈 Tendance offensive forte. Conseil : Jouer 'Over {ligne} buts'.")
        elif deux_excellents:
            ligne = _arrondir_au_demi(total + 0.5)
            conseils.append(f"📉 Match défensif anticipé. Conseil : Jouer 'Under {ligne} buts'.")
        elif total >= seuils["buts_total_haut"]:
            ligne = _arrondir_au_demi(total - 0.5)
            conseils.append(f"📈 Projection au seuil haut. Conseil : Jouer 'Over {ligne} buts'.")
        else:
            ligne = _arrondir_au_demi(total + 0.5)
            conseils.append(f"📉 Projection contenue. Conseil : Jouer 'Under {ligne} buts'.")

    if joueurs_a_surveiller:
        meilleur = joueurs_a_surveiller[0]
        if meilleur.get("confiance") in ("Élevée", "Moyenne"):
            conseils.append(
                f"🎯 Option alternative : {meilleur['nom']} a une forte probabilité "
                "de marquer un but ou une passe aujourd'hui."
            )
    return conseils


def _filtrer_phrases_over_under_conseils(conseils) -> list:
    marqueurs = ("Jouer 'Over", "Jouer 'Under", "Tendance offensive", "Match défensif", "Projection")
    return [c for c in (conseils or []) if not any(m in c for m in marqueurs)]


# ---------------------------------------------------------------------------
# Odds / Value
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_cotes_moneyline_du_jour(sport_key: str, api_key: str):
    if not api_key:
        return []
    try:
        reponse = requests.get(
            "https://api.the-odds-api.com/v4/sports/{}/odds".format(sport_key),
            params={
                "apiKey": api_key,
                "regions": "eu,uk",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            timeout=10,
        )
        if not reponse.ok:
            return []
        matchs_api = reponse.json() or []
    except Exception:
        return []

    resultats = []
    for match in matchs_api:
        bookmakers = match.get("bookmakers") or []
        if not bookmakers:
            continue
        book = bookmakers[0]
        market = next((m for m in book.get("markets", []) if m.get("key") == "h2h"), None)
        if not market:
            continue
        outcomes = {o.get("name"): o.get("price") for o in market.get("outcomes") or []}
        home = match.get("home_team")
        away = match.get("away_team")
        if home not in outcomes or away not in outcomes:
            continue
        resultats.append({
            "home": home,
            "away": away,
            "cote_nous": outcomes[home],
            "cote_adverse": outcomes[away],
            "bookmaker": book.get("title") or "Bookmaker",
        })
    return resultats


def trouver_cote_du_match(cotes_du_jour, home_name, away_name):
    nh, na = _normaliser_nom(home_name), _normaliser_nom(away_name)
    for c in cotes_du_jour or []:
        ch, ca = _normaliser_nom(c.get("home")), _normaliser_nom(c.get("away"))
        if (nh in ch or ch in nh) and (na in ca or ca in na):
            return c
        if (nh in ca or ca in nh) and (na in ch or ch in na):
            return {
                "home": c.get("away"),
                "away": c.get("home"),
                "cote_nous": c.get("cote_adverse"),
                "cote_adverse": c.get("cote_nous"),
                "bookmaker": c.get("bookmaker"),
            }
    return None


def evaluer_value_bet(proba_pct, cote, nom_equipe, bookmaker):
    if proba_pct is None or cote is None:
        return "none", ""
    try:
        p = float(proba_pct) / 100.0
        c = float(cote)
    except (TypeError, ValueError):
        return "none", ""
    if c <= 1:
        return "none", ""
    implied = 1.0 / c
    edge = p - implied
    if edge >= 0.05:
        return "value", f"Value Bet détectée sur {nom_equipe} @{c:.2f} ({bookmaker})"
    if edge >= 0.015:
        return "juste", f"Cote correcte sur {nom_equipe} @{c:.2f}"
    return "evitez", f"Cote insuffisante sur {nom_equipe} @{c:.2f}"


# ---------------------------------------------------------------------------
# Totaux / écarts / classements joueurs
# ---------------------------------------------------------------------------
def _total_buts_predit(gf_home, gf_away):
    if gf_home is None or gf_away is None or pd.isna(gf_home) or pd.isna(gf_away):
        return None
    return round(float(gf_home) + float(gf_away), 2)


def _ecart_points_predit(buts_home, buts_away):
    if buts_home is None or buts_away is None or pd.isna(buts_home) or pd.isna(buts_away):
        return None
    return round(float(buts_home) - float(buts_away), 2)


def classer_recommandation_totaux_over_under(total_projete, ligne):
    if total_projete is None or ligne is None:
        return None
    try:
        total = float(total_projete)
        cut = float(ligne)
    except (TypeError, ValueError):
        return None
    if pd.isna(total) or pd.isna(cut):
        return None
    ecart = total - cut
    resume = f"Proj: {total:.1f} | Ligne: {cut:.1f}"
    if abs(ecart) <= 0.5:
        return {"code": "NO_BET", "resume": f"{resume} - marge trop faible"}
    if ecart > 0.5:
        return {"code": "OVER", "resume": resume}
    return {"code": "UNDER", "resume": resume}


def formater_recommandation_totaux_over_under(total_projete, ligne):
    classement = classer_recommandation_totaux_over_under(total_projete, ligne)
    if not classement:
        return None
    total = float(total_projete)
    cut = float(ligne)
    if classement["code"] == "NO_BET":
        return (
            f"⚠️ **Recommandation Totaux : NO BET sur les buts** "
            f"(Projection : {total:.1f} | Ligne : {cut:.1f} - marge trop faible)."
        )
    return (
        f"📊 **Recommandation Totaux : Jouer l'{classement['code']}** "
        f"(Projection : {total:.1f} | Ligne : {cut:.1f})."
    )


def formater_ecart_points(ecart, home_name, away_name, pct_home, pct_away):
    """Reco d'écart (spread) possible pour le match."""
    if ecart is None or pd.isna(ecart):
        return None
    ecart = float(ecart)
    # Favori selon proba si dispo, sinon selon signe de l'écart (home positif)
    if pct_home is not None and pct_away is not None:
        favori = home_name if pct_home >= pct_away else away_name
        signe_favori = ecart if favori == home_name else -ecart
    else:
        favori = home_name if ecart >= 0 else away_name
        signe_favori = abs(ecart)

    spread = round(abs(ecart) * 2) / 2  # au demi-but
    if spread < 0.5:
        return {
            "ecart_kind": "NO_BET",
            "ecart_label": "⚠️ Serré",
            "ecart_resume": f"Écart estimé {ecart:+.1f} — trop serré pour un spread",
            "ecart_valeur": ecart,
        }
    return {
        "ecart_kind": None,
        "ecart_label": f"🎯 {favori} -{spread:.1f}",
        "ecart_resume": f"Écart estimé {ecart:+.1f} (home − away) · spread suggéré {spread:.1f}",
        "ecart_valeur": ecart,
    }


@st.cache_data(show_spinner=False, ttl=3600)
def obtenir_ligne_over_under_saison() -> float:
    """Moyenne buts/match (2 équipes) via standings saison."""
    df = obtenir_standings_nhl()
    if df.empty:
        return 6.0
    totaux = (df["gf"] + df["ga"]) / df["gp"].clip(lower=1)
    # Chaque match compté 2 fois (une par équipe) → moyenne des totaux ≈ mean(gf_pg+ga_pg)
    return round(float(totaux.mean()), 2)


def _normaliser_colonne(serie: pd.Series) -> pd.Series:
    s = pd.to_numeric(serie, errors="coerce")
    if s.isna().all():
        return pd.Series([0.5] * len(s), index=s.index)
    mn, mx = s.min(), s.max()
    if pd.isna(mn) or pd.isna(mx) or mx == mn:
        return pd.Series([0.5] * len(s), index=s.index)
    return (s - mn) / (mx - mn)


def _classer_buteurs(candidats: list) -> pd.DataFrame:
    if not candidats:
        return pd.DataFrame()
    df = pd.DataFrame(candidats)
    indice = (
        _normaliser_colonne(df["goals_10"]) * 0.45
        + _normaliser_colonne(df["goals_saison_pg"]) * 0.30
        + _normaliser_colonne(df["gaa_adverse"]) * 0.25
    ) * 100
    df["Indice But (/100)"] = indice.round(0)
    return df.sort_values("Indice But (/100)", ascending=False).reset_index(drop=True)


def _classer_passeurs(candidats: list) -> pd.DataFrame:
    if not candidats:
        return pd.DataFrame()
    df = pd.DataFrame(candidats)
    # Position haute (C/ailes) légèrement favorisée via points
    indice = (
        _normaliser_colonne(df["assists_10"]) * 0.50
        + _normaliser_colonne(df["assists_saison_pg"]) * 0.30
        + _normaliser_colonne(df["points_10"]) * 0.20
    ) * 100
    df["Indice Passe (/100)"] = indice.round(0)
    return df.sort_values("Indice Passe (/100)", ascending=False).reset_index(drop=True)


def _meilleure_reco_joueur_match(df_all, home, away, indice_col):
    if df_all is None or getattr(df_all, "empty", True):
        return None
    masque = (
        ((df_all["Équipe"] == home) & (df_all["Adversaire"] == away))
        | ((df_all["Équipe"] == away) & (df_all["Adversaire"] == home))
    )
    sous = df_all[masque]
    if sous.empty:
        return None
    best = sous.iloc[0]
    return {
        "joueur": best.get("Joueur"),
        "equipe": best.get("Équipe"),
        "indice": best.get(indice_col),
    }


def _projeter_buts_equipes(abbr_home, abbr_away):
    forme_h = obtenir_forme_equipe(abbr_home)
    forme_a = obtenir_forme_equipe(abbr_away)
    gard_h = obtenir_meilleur_gardien(abbr_home)
    gard_a = obtenir_meilleur_gardien(abbr_away)
    # Attaque home vs gardien away
    buts_home = forme_h["gf"]
    buts_away = forme_a["gf"]
    if buts_home is not None and gard_a and gard_a.get("gaa"):
        buts_home = (buts_home * 0.6) + (gard_a["gaa"] * 0.4)
        if (gard_a.get("sv_pct") or 0.9) >= 0.920:
            buts_home *= 0.92
    if buts_away is not None and gard_h and gard_h.get("gaa"):
        buts_away = (buts_away * 0.6) + (gard_h["gaa"] * 0.4)
        if (gard_h.get("sv_pct") or 0.9) >= 0.920:
            buts_away *= 0.92
    return buts_home, buts_away, forme_h, forme_a, gard_h, gard_a


# ---------------------------------------------------------------------------
# Hot Pronostics global
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=1800)
def construire_donnees_hot_pronostics_nhl():
    games, date_ref, source = obtenir_scores_du_jour()
    if not games:
        return [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), date_ref, source

    matchs = []
    candidats_but = []
    candidats_passe = []
    lignes_victoire = []

    for g in games:
        home = g.get("homeTeam") or {}
        away = g.get("awayTeam") or {}
        home_abbr = (home.get("abbrev") or "").upper()
        away_abbr = (away.get("abbrev") or "").upper()
        home_name = _texte_localise(home.get("name") or home.get("commonName") or home.get("placeName")) or home_abbr
        away_name = _texte_localise(away.get("name") or away.get("commonName") or away.get("placeName")) or away_abbr
        # Compléter noms via standings
        teams = get_teams_nhl_dict()
        home_name = teams.get(home_abbr, home_name)
        away_name = teams.get(away_abbr, away_name)

        heure_us, heure_paris = _heure_paris_depuis_utc(g.get("startTimeUTC"))
        buts_home, buts_away, forme_h, forme_a, gard_h, gard_a = _projeter_buts_equipes(home_abbr, away_abbr)

        pct_home, pct_away = predire_probabilite_victoire(
            forme_h.get("gf"),
            forme_a.get("gf"),
            gard_h,
            gard_a,
            est_domicile=True,
        )
        lignes_victoire.append({
            "Heure (France)": heure_paris,
            "Équipe Domicile": home_name,
            "Gardien Domicile": (gard_h or {}).get("nom") or "Non annoncé",
            "Équipe Extérieur": away_name,
            "Gardien Extérieur": (gard_a or {}).get("nom") or "Non annoncé",
            "Proba Domicile (%)": pct_home,
            "Proba Extérieur (%)": pct_away,
            "Buts proj. domicile": None if buts_home is None else round(buts_home, 2),
            "Buts proj. extérieur": None if buts_away is None else round(buts_away, 2),
        })

        matchs.append({
            "game_id": g.get("id"),
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "home_name": home_name,
            "away_name": away_name,
            "heure_paris": heure_paris,
            "heure_us": heure_us,
            "statut": _statut_match(g.get("gameState")),
            "buts_home_proj": None if buts_home is None else round(buts_home, 2),
            "buts_away_proj": None if buts_away is None else round(buts_away, 2),
        })

        for camp_abbr, camp_name, adv_name, gard_adv in (
            (home_abbr, home_name, away_name, gard_a),
            (away_abbr, away_name, home_name, gard_h),
        ):
            for s in top_skaters_equipe(camp_abbr, top_n=8, avec_gamelog=False):
                gp_s = max(1, s["gp_saison"])
                candidats_but.append({
                    "Joueur": s["nom"],
                    "Équipe": camp_name,
                    "Adversaire": adv_name,
                    "Poste": s["poste"],
                    "goals_10": s["goals_10"],
                    "goals_saison_pg": s["goals_saison"] / gp_s,
                    "gaa_adverse": (gard_adv or {}).get("gaa") or 2.9,
                })
                candidats_passe.append({
                    "Joueur": s["nom"],
                    "Équipe": camp_name,
                    "Adversaire": adv_name,
                    "Poste": s["poste"],
                    "assists_10": s["assists_10"],
                    "assists_saison_pg": s["assists_saison"] / gp_s,
                    "points_10": s["points_10"],
                })

    df_but_all = _classer_buteurs(candidats_but)
    df_passe_all = _classer_passeurs(candidats_passe)
    df_top5_but = df_but_all.head(5).reset_index(drop=True) if not df_but_all.empty else df_but_all
    df_top5_passe = df_passe_all.head(5).reset_index(drop=True) if not df_passe_all.empty else df_passe_all
    df_victoires = pd.DataFrame(lignes_victoire)

    # Snapshot historique
    matches_snapshot = []
    for m, v in zip(matchs, lignes_victoire.to_dict("records") if not df_victoires.empty else []):
        matches_snapshot.append({
            "game_id": m.get("game_id"),
            "home_name": m["home_name"],
            "away_name": m["away_name"],
            "proba_home": v.get("Proba Domicile (%)"),
            "proba_away": v.get("Proba Extérieur (%)"),
            "total_buts_predit": _total_buts_predit(m.get("buts_home_proj"), m.get("buts_away_proj")),
            "ecart_predit": _ecart_points_predit(m.get("buts_home_proj"), m.get("buts_away_proj")),
        })
    _sauvegarder_predictions_du_jour(date_ref, matches_snapshot)

    return matchs, df_top5_but, df_top5_passe, df_victoires, df_but_all, df_passe_all, date_ref, source


def assembler_lignes_recap_hot_pronostics(matchs_jour, df_victoires, df_but_all, df_passe_all) -> list:
    if not matchs_jour or df_victoires is None or getattr(df_victoires, "empty", True):
        return []
    ligne_ou = obtenir_ligne_over_under_saison()
    cle_odds = _lire_cle_odds_api()
    cotes_du_jour = obtenir_cotes_moneyline_du_jour(ODDS_API_SPORT_KEY, cle_odds) if cle_odds else []

    lignes = []
    for idx, m in enumerate(matchs_jour):
        if idx >= len(df_victoires):
            break
        v = df_victoires.iloc[idx]
        home, away = m["home_name"], m["away_name"]
        pct_home, pct_away = v.get("Proba Domicile (%)"), v.get("Proba Extérieur (%)")
        favori, pct_fav = None, None
        if pct_home is not None and pct_away is not None:
            if pct_home >= pct_away:
                favori, pct_fav = home, float(pct_home)
            else:
                favori, pct_fav = away, float(pct_away)

        value_kind, value_label = "none", "Pas de value"
        if favori and cotes_du_jour:
            cotes_match = trouver_cote_du_match(cotes_du_jour, home, away)
            if cotes_match:
                cote_fav = cotes_match.get("cote_nous") if favori == home else cotes_match.get("cote_adverse")
                niveau, _msg = evaluer_value_bet(pct_fav, cote_fav, favori, cotes_match.get("bookmaker") or "Bookmaker")
                value_kind = {"value": "value", "juste": "medium", "evitez": "avoid"}.get(niveau, "none")
                value_label = {"value": "Value forte", "juste": "Value moyenne", "evitez": "Pas de value"}.get(niveau, "Pas de value")

        total_proj = _total_buts_predit(m.get("buts_home_proj"), m.get("buts_away_proj"))
        classement_ou = classer_recommandation_totaux_over_under(total_proj, ligne_ou)
        ecart = _ecart_points_predit(m.get("buts_home_proj"), m.get("buts_away_proj"))
        info_ecart = formater_ecart_points(ecart, home, away, pct_home, pct_away) or {}

        reco_but = _meilleure_reco_joueur_match(df_but_all, home, away, "Indice But (/100)")
        reco_passe = _meilleure_reco_joueur_match(df_passe_all, home, away, "Indice Passe (/100)")

        lignes.append({
            "confrontation": f"{away} vs {home}",
            "heure": f"⏰ {m.get('heure_paris') or '—'}",
            "favori": favori,
            "favori_pct": pct_fav,
            "value_kind": value_kind,
            "value_label": value_label,
            "ou_kind": classement_ou["code"] if classement_ou else None,
            "ou_resume": classement_ou["resume"] if classement_ou else "Projection indisponible",
            "ecart_kind": info_ecart.get("ecart_kind"),
            "ecart_label": info_ecart.get("ecart_label"),
            "ecart_resume": info_ecart.get("ecart_resume"),
            "reco_hr": f"{reco_but['joueur']} ({reco_but['equipe']})" if reco_but and reco_but.get("joueur") else None,
            "reco_hr_detail": f"Indice {reco_but['indice']:.0f}/100" if reco_but and reco_but.get("indice") is not None else None,
            "reco_run": f"{reco_passe['joueur']} ({reco_passe['equipe']})" if reco_passe and reco_passe.get("joueur") else None,
            "reco_run_detail": f"Indice {reco_passe['indice']:.0f}/100" if reco_passe and reco_passe.get("indice") is not None else None,
        })
    return lignes


# ---------------------------------------------------------------------------
# Résumé / match du jour
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=300)
def construire_resume_matchs_du_jour(cache_bust: int = 0):
    games, date_ref, source = obtenir_scores_du_jour()
    if not games:
        return pd.DataFrame(), None, date_ref, source

    try:
        matchs_hot, _, _, df_victoires, *_rest = construire_donnees_hot_pronostics_nhl()
    except Exception:
        matchs_hot, df_victoires = [], pd.DataFrame()

    pred_by_id = {}
    for idx, m in enumerate(matchs_hot or []):
        if idx < len(df_victoires):
            pred_by_id[m.get("game_id")] = df_victoires.iloc[idx]

    lignes = []
    for g in games:
        home = g.get("homeTeam") or {}
        away = g.get("awayTeam") or {}
        home_abbr = (home.get("abbrev") or "").upper()
        away_abbr = (away.get("abbrev") or "").upper()
        teams = get_teams_nhl_dict()
        home_name = teams.get(home_abbr, _texte_localise(home.get("name") or home.get("commonName")) or home_abbr)
        away_name = teams.get(away_abbr, _texte_localise(away.get("name") or away.get("commonName")) or away_abbr)
        statut = _statut_match(g.get("gameState"))
        a_commence = statut in ("Terminé", "En cours")
        hs = home.get("score")
        as_ = away.get("score")
        if a_commence and hs is not None and as_ is not None:
            score_str = f"{away_abbr} {as_} - {home_abbr} {hs}"
            total = int(as_) + int(hs)
            ecart_reel = int(hs) - int(as_)
        else:
            score_str, total, ecart_reel = "—", "—", "—"

        pred = pred_by_id.get(g.get("id"))
        if pred is not None:
            ph, pa = pred.get("Proba Domicile (%)"), pred.get("Proba Extérieur (%)")
            fav = home_name if ph >= pa else away_name
            comparatif = f"{fav} ({max(ph, pa):.0f}%)"
            if a_commence and hs is not None and as_ is not None:
                meneur = home_name if int(hs) > int(as_) else (away_name if int(as_) > int(hs) else None)
                resultat = "⏳" if meneur is None else ("✅" if meneur == fav else "❌")
            else:
                resultat = "⏳"
        else:
            comparatif, resultat = "—", "⏳"

        _, heure_paris = _heure_paris_depuis_utc(g.get("startTimeUTC"))
        lignes.append({
            "Match": f"{away_name} @ {home_name}",
            "Statut": statut if statut != "À venir" else f"À venir · {heure_paris}",
            "Score": score_str,
            "Total Buts": total,
            "Écart": ecart_reel,
            "Comparatif Prédiction": comparatif,
            "Résultat vs Algo": resultat,
        })
    return pd.DataFrame(lignes), None, date_ref, source


def obtenir_match_du_jour(abbr: str):
    abbr = (abbr or "").upper()
    games, date_ref, source = obtenir_scores_du_jour()
    for g in games:
        home = g.get("homeTeam") or {}
        away = g.get("awayTeam") or {}
        if (home.get("abbrev") or "").upper() == abbr or (away.get("abbrev") or "").upper() == abbr:
            est_domicile = (home.get("abbrev") or "").upper() == abbr
            adv_abbr = (away.get("abbrev") if est_domicile else home.get("abbrev") or "").upper()
            teams = get_teams_nhl_dict()
            heure_us, heure_paris = _heure_paris_depuis_utc(g.get("startTimeUTC"))
            venue = _texte_localise((g.get("venue") or {})) if isinstance(g.get("venue"), dict) else (g.get("venue") or "—")
            return {
                "game_id": g.get("id"),
                "est_domicile": est_domicile,
                "abbr": abbr,
                "adversaire_abbr": adv_abbr,
                "adversaire": teams.get(adv_abbr, adv_abbr),
                "home_abbr": (home.get("abbrev") or "").upper(),
                "away_abbr": (away.get("abbrev") or "").upper(),
                "heure_us": heure_us,
                "heure_paris": heure_paris,
                "statut": _statut_match(g.get("gameState")),
                "venue": venue or "—",
                "date_ref": date_ref,
                "source": source,
                "ligue": "NHL",
            }
    return None


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
render_page_header(
    "Analyse Statistiques NHL",
    "Buteurs, passeurs, totaux de buts et écarts de points",
    league="nhl",
)

EQUIPES_NHL = get_teams_nhl_dict()
saison_id = saison_stats_courante()

with st.sidebar:
    st.header("⚙️ Paramètres")
    abbrs = sorted(EQUIPES_NHL.keys())
    equipe_abbr = st.selectbox(
        "Sélectionnez une équipe:",
        options=abbrs,
        format_func=lambda a: f"{a} — {EQUIPES_NHL.get(a, a)}",
        index=abbrs.index("TOR") if "TOR" in abbrs else 0,
    )
    st.markdown("---")
    st.markdown("**Légende:**")
    st.markdown("""
    - **Buteur** : joueur susceptible de marquer un but
    - **Passeur** : joueur susceptible d'offrir une passe décisive
    - **GF / GA** : Buts pour / Buts contre
    - **GAA / SV%** : Moyenne de buts encaissés / % d'arrêts
    - **Écart** : différence de buts projetée (spread)
    """)
    st.caption(f"Saison stats : {saison_id}")

onglets = st.tabs([
    "📊 Résumé",
    "🔥 Hot Pronostics",
    "📊 Analyse par Équipe",
    "🔮 Prédictions du jour",
], on_change="rerun")

# ---- Résumé ----
with onglets[0]:
    if onglets[0].open:
        render_section_title("Résumé du jour", "Scores NHL et comparatif aux prédictions")
        if "resume_cache_bust" not in st.session_state:
            st.session_state.resume_cache_bust = 0
        if st.button("🔄 Rafraîchir les scores"):
            st.session_state.resume_cache_bust += 1
        with st.spinner("Récupération des scores..."):
            df_resume, err, date_ref, source = construire_resume_matchs_du_jour(
                st.session_state.resume_cache_bust
            )
        if source == "prochains":
            st.info(f"Aucun match aujourd'hui (ET). Affichage des prochains matchs à partir du {date_ref}.")
        else:
            st.caption(f"Date de référence (heure de l'Est) : {date_ref}")
        if err:
            st.error(err)
        elif df_resume.empty:
            st.info("Aucun match NHL à afficher pour le moment.")
        else:
            afficher_cartes_matchs(
                df_resume,
                show_table_fallback=True,
                column_config={
                    "Match": st.column_config.TextColumn("Match", width="medium"),
                    "Statut": st.column_config.TextColumn("Statut", width="medium"),
                    "Score": st.column_config.TextColumn("Score", width="small"),
                    "Total Buts": st.column_config.TextColumn("Total Buts", width="small"),
                    "Écart": st.column_config.TextColumn("Écart", width="small"),
                    "Comparatif Prédiction": st.column_config.TextColumn("Comparatif Prédiction", width="medium"),
                    "Résultat vs Algo": st.column_config.TextColumn("Résultat vs Algo", width="small"),
                },
            )

# ---- Hot Pronostics ----
with onglets[1]:
    if onglets[1].open:
        render_section_title(
            "Hot Pronostics du jour",
            "Buteurs, passeurs, totaux de buts et écarts de points — tous matchs",
        )
        with st.spinner("Analyse des matchs NHL (forme L10, gardiens, top skaters)..."):
            (
                matchs_jour, df_top5_but, df_top5_passe, df_victoires,
                df_but_all, df_passe_all, date_ref, source,
            ) = construire_donnees_hot_pronostics_nhl()
            lignes_recap = assembler_lignes_recap_hot_pronostics(
                matchs_jour, df_victoires, df_but_all, df_passe_all
            )

        if source == "prochains":
            st.info(f"Intersaison / jour sans match : pronostics basés sur les prochains matchs ({date_ref}).")

        if not matchs_jour:
            st.info("Aucun match NHL programmé pour alimenter les Hot Pronostics.")
        else:
            st.subheader("📋 Tableau de bord du jour")
            afficher_tableau_recap_hot_pronostics(
                lignes_recap,
                label_joueurs="Joueurs (Buteur / Passeur)",
                label_primary="🏒 Buteur",
                label_secondary="🎯 Passeur",
                show_ecart=True,
            )
            st.caption(
                "Totaux (O/U) : somme des buts projetés des 2 équipes vs ligne saison. "
                "Écart de points : différence domicile − extérieur (spread suggéré au demi-but)."
            )
            st.caption(
                "⚠️ Heuristiques automatiques (forme récente, GAA/SV% des gardiens titulaires "
                "estimés, production des top skaters). Pas de garanties de résultat."
            )

            st.markdown("---")
            st.subheader("🏒 Top 5 Buteurs probables")
            if df_top5_but.empty:
                st.info("Pas assez de données skaters pour classer les buteurs.")
            else:
                st.dataframe(
                    df_top5_but[[
                        "Joueur", "Équipe", "Adversaire", "Poste",
                        "goals_10", "goals_saison_pg", "gaa_adverse", "Indice But (/100)",
                    ]].rename(columns={
                        "goals_10": "Buts (10 derniers)",
                        "goals_saison_pg": "Buts/match saison",
                        "gaa_adverse": "GAA gardien adv.",
                    }),
                    hide_index=True,
                    column_config={
                        "Buts/match saison": st.column_config.NumberColumn(format="%.2f"),
                        "GAA gardien adv.": st.column_config.NumberColumn(format="%.2f"),
                        "Indice But (/100)": st.column_config.ProgressColumn(
                            "Indice But (/100)", min_value=0, max_value=100, format="%.0f"
                        ),
                    },
                )

            st.subheader("🎯 Top 5 Passeurs probables")
            if df_top5_passe.empty:
                st.info("Pas assez de données skaters pour classer les passeurs.")
            else:
                st.dataframe(
                    df_top5_passe[[
                        "Joueur", "Équipe", "Adversaire", "Poste",
                        "assists_10", "assists_saison_pg", "points_10", "Indice Passe (/100)",
                    ]].rename(columns={
                        "assists_10": "Passes (10 derniers)",
                        "assists_saison_pg": "Passes/match saison",
                        "points_10": "Points (10 derniers)",
                    }),
                    hide_index=True,
                    column_config={
                        "Passes/match saison": st.column_config.NumberColumn(format="%.2f"),
                        "Indice Passe (/100)": st.column_config.ProgressColumn(
                            "Indice Passe (/100)", min_value=0, max_value=100, format="%.0f"
                        ),
                    },
                )

            st.subheader("🏆 Probabilités de victoire")
            st.dataframe(df_victoires, hide_index=True)

# ---- Analyse par équipe ----
with onglets[2]:
    if onglets[2].open:
        render_section_title(
            f"Analyse — {EQUIPES_NHL.get(equipe_abbr, equipe_abbr)}",
            "Classement, gardiens et production offensive",
        )
        df_stand = obtenir_standings_nhl()
        row = df_stand[df_stand["abbr"] == equipe_abbr]
        if row.empty:
            st.warning("Équipe introuvable dans les standings NHL.")
        else:
            r = row.iloc[0]
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Points", int(r["pts"]))
            c2.metric("Fiche", f"{int(r['w'])}-{int(r['l'])}-{int(r['otl'])}")
            c3.metric("GF / match", f"{r['gf_pg']:.2f}")
            c4.metric("GA / match", f"{r['ga_pg']:.2f}")
            c5.metric("Diff", int(r["gf"] - r["ga"]))
            st.caption(f"{r['conference']} · {r['division']} · forme L10 : {int(r['l10_w'])}-{int(r['l10_gp'])-int(r['l10_w'])} (approx)")

            forme = obtenir_forme_equipe(equipe_abbr)
            st.info(
                f"Forme utilisée pour les prédictions ({forme['source']}) : "
                f"**{forme['gf']:.2f}** GF/match · **{forme['ga']:.2f}** GA/match"
            )

            gard = obtenir_meilleur_gardien(equipe_abbr)
            if gard:
                st.subheader("🧊 Gardien de référence")
                st.markdown(
                    f"**{gard['nom']}** — GAA {gard['gaa']:.2f} · SV% {gard['sv_pct']:.3f} · "
                    f"{gard['wins']} V en {gard['starts']} départs"
                )

            st.subheader("📈 Top skaters (saison + forme 10 matchs)")
            skaters = top_skaters_equipe(equipe_abbr, top_n=15)
            if not skaters:
                st.info("Stats skaters indisponibles.")
            else:
                df_s = pd.DataFrame(skaters)
                st.dataframe(
                    df_s[[
                        "nom", "poste", "gp_saison", "goals_saison", "assists_saison",
                        "points_saison", "goals_10", "assists_10", "points_10",
                    ]].rename(columns={
                        "nom": "Joueur", "poste": "Poste", "gp_saison": "MJ",
                        "goals_saison": "B", "assists_saison": "A", "points_saison": "Pts",
                        "goals_10": "B (10)", "assists_10": "A (10)", "points_10": "Pts (10)",
                    }),
                    hide_index=True,
                )
                chart_df = df_s.head(8).melt(
                    id_vars=["nom"], value_vars=["goals_10", "assists_10"],
                    var_name="type", value_name="n",
                )
                chart_df["type"] = chart_df["type"].map({"goals_10": "Buts", "assists_10": "Passes"})
                st.altair_chart(
                    alt.Chart(chart_df).mark_bar().encode(
                        x=alt.X("nom:N", sort="-y", title=None),
                        y=alt.Y("n:Q", title="Sur 10 matchs"),
                        color=alt.Color("type:N", title=None),
                        tooltip=["nom", "type", "n"],
                    ).properties(height=280),
                    use_container_width=True,
                )

# ---- Prédictions du jour ----
with onglets[3]:
    if onglets[3].open:
        render_section_title(
            "Prédictions du jour",
            f"Match de {EQUIPES_NHL.get(equipe_abbr, equipe_abbr)}",
        )
        st.caption(
            "⚠️ Estimations basées sur la forme récente (L10 / saison) et le profil des gardiens. "
            "À titre informatif uniquement."
        )
        with st.spinner("Recherche du match..."):
            match = obtenir_match_du_jour(equipe_abbr)
        if not match:
            st.info(f"Aucun match (jour ou prochain) trouvé pour {EQUIPES_NHL.get(equipe_abbr, equipe_abbr)}.")
        else:
            if match.get("source") == "prochains":
                st.info(f"Prochain match détecté ({match['date_ref']}), aucun match aujourd'hui.")
            lieu = "à domicile" if match["est_domicile"] else "à l'extérieur"
            render_prediction_match_banner(
                f"{EQUIPES_NHL.get(equipe_abbr, equipe_abbr)} {lieu} contre {match['adversaire']}",
                "Gardiens · probabilités · totaux · écart · value",
            )
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Patinoire", match["venue"])
            c2.metric("Heure (US Est)", match["heure_us"])
            c3.metric("Heure (France)", match["heure_paris"])
            c4.metric("Statut", match["statut"])

            forme_nous = obtenir_forme_equipe(equipe_abbr)
            forme_adv = obtenir_forme_equipe(match["adversaire_abbr"])
            gard_nous = obtenir_meilleur_gardien(equipe_abbr)
            gard_adv = obtenir_meilleur_gardien(match["adversaire_abbr"])

            st.markdown("#### 🧊 Gardiens de référence")
            g1, g2 = st.columns(2)
            with g1:
                st.markdown(f"**{EQUIPES_NHL.get(equipe_abbr, equipe_abbr)}**")
                st.markdown(f"### {(gard_nous or {}).get('nom') or 'Non disponible'}")
                if gard_nous:
                    st.caption(f"GAA {gard_nous['gaa']:.2f} · SV% {gard_nous['sv_pct']:.3f}")
            with g2:
                st.markdown(f"**{match['adversaire']}**")
                st.markdown(f"### {(gard_adv or {}).get('nom') or 'Non disponible'}")
                if gard_adv:
                    st.caption(f"GAA {gard_adv['gaa']:.2f} · SV% {gard_adv['sv_pct']:.3f}")

            st.markdown("---")
            st.subheader("🎲 Probabilité de Victoire")
            pct_nous, pct_adverse = predire_probabilite_victoire(
                forme_nous.get("gf"),
                forme_adv.get("gf"),
                gard_nous,
                gard_adv,
                match["est_domicile"],
            )
            p1, p2 = st.columns(2)
            p1.metric(EQUIPES_NHL.get(equipe_abbr, equipe_abbr), f"{pct_nous:.0f}%")
            p2.metric(match["adversaire"], f"{pct_adverse:.0f}%")
            st.progress(pct_nous / 100)

            prediction_buts = predire_buts_match(
                forme_nous.get("gf"), forme_nous.get("ga"), gard_adv
            )
            skaters = top_skaters_equipe(equipe_abbr, top_n=12)
            joueurs = predire_joueurs_du_jour(skaters, gard_adv, top_n=3)

            conseils = generer_recommandation_pari(
                pct_nous, pct_adverse, gard_nous, gard_adv, prediction_buts, joueurs, ligue="NHL",
            )

            # Projection MATCH (alignée Hot Pronostics)
            if match["est_domicile"]:
                buts_home, buts_away, *_ = _projeter_buts_equipes(equipe_abbr, match["adversaire_abbr"])
            else:
                buts_home, buts_away, *_ = _projeter_buts_equipes(match["adversaire_abbr"], equipe_abbr)
            total_match_hot = _total_buts_predit(buts_home, buts_away)
            total_vue = prediction_buts.get("total_match") if prediction_buts else None
            ligne_ou = obtenir_ligne_over_under_saison()
            classement_match = classer_recommandation_totaux_over_under(total_match_hot, ligne_ou)
            classement_vue = classer_recommandation_totaux_over_under(total_vue, ligne_ou)
            reco_totaux = formater_recommandation_totaux_over_under(total_match_hot, ligne_ou)

            ecart = _ecart_points_predit(buts_home, buts_away)
            # Écart du point de vue de l'équipe sélectionnée
            if match["est_domicile"]:
                info_ecart = formater_ecart_points(ecart, EQUIPES_NHL.get(equipe_abbr, equipe_abbr), match["adversaire"], pct_nous, pct_adverse)
            else:
                info_ecart = formater_ecart_points(ecart, match["adversaire"], EQUIPES_NHL.get(equipe_abbr, equipe_abbr), pct_adverse, pct_nous)

            lignes_reco = _filtrer_phrases_over_under_conseils(conseils)
            if reco_totaux:
                lignes_reco.append(reco_totaux)
            if info_ecart:
                lignes_reco.append(
                    f"📏 **Écart de points possible :** {info_ecart.get('ecart_label')} — {info_ecart.get('ecart_resume')}"
                )
            if lignes_reco:
                st.info("**💡 Recommandation de Pari Optimisée**\n\n" + "\n\n".join(lignes_reco))

            afficher_outil_coherence_totaux(
                total_match_hot,
                total_vue,
                ligne_ou,
                code_match=classement_match["code"] if classement_match else None,
                code_vue=classement_vue["code"] if classement_vue else None,
            )

            # Value bet
            cle = _lire_cle_odds_api()
            if cle:
                home_n = EQUIPES_NHL.get(match["home_abbr"], match["home_abbr"])
                away_n = EQUIPES_NHL.get(match["away_abbr"], match["away_abbr"])
                cotes = obtenir_cotes_moneyline_du_jour(ODDS_API_SPORT_KEY, cle)
                cm = trouver_cote_du_match(cotes, home_n, away_n)
                if cm:
                    cote_nous = cm["cote_nous"] if match["est_domicile"] else cm["cote_adverse"]
                    niveau, msg = evaluer_value_bet(pct_nous, cote_nous, EQUIPES_NHL.get(equipe_abbr, equipe_abbr), cm["bookmaker"])
                    afficher_badge_value_bet(niveau, msg)
                else:
                    st.caption("Cotes moneyline introuvables pour ce match (Odds API).")
            else:
                st.caption("Ajoutez `[odds_api] api_key` dans `.streamlit/secrets.toml` pour le Value Bet.")

            st.markdown("---")
            st.subheader("🏒 Prédiction des buts")
            if prediction_buts:
                m1, m2, m3 = st.columns(3)
                m1.metric("Buts estimés (équipe)", prediction_buts["buts_equipe"])
                m2.metric("Total buts estimé (match)", prediction_buts["total_match"])
                m3.metric("Confiance", prediction_buts["confiance"])
                st.caption(
                    f"Projection match Hot : **{total_match_hot}** buts · "
                    f"écart domicile−extérieur : **{ecart:+.1f}**" if total_match_hot is not None and ecart is not None
                    else "Projection match indisponible."
                )

            st.subheader("⭐ Joueurs à surveiller")
            if not joueurs:
                st.info("Pas assez de données joueurs.")
            else:
                st.dataframe(pd.DataFrame(joueurs), hide_index=True)

render_footer("NHL", datetime.now(TZ_PARIS).strftime("%d/%m/%Y %H:%M"))
