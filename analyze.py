#!/usr/bin/env python3
"""Analyze Chess.com PGN export and build dashboard JSON."""

from __future__ import annotations

import argparse
import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import chess.pgn

USERNAME = "netrebnic"
TZ = ZoneInfo("Europe/Bucharest")
MIN_OPENING_GAMES = 15
MIN_HOUR_GAMES = 40
MIN_HEATMAP_GAMES = 8
MIN_TC_GAMES_HIGH = 100
MIN_TC_GAMES_MEDIUM = 50

WEEKDAY_ORDER = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Stylized archetype fingerprints (0–1). Used for legend similarity only.
LEGEND_ARCHETYPES: dict[str, dict[str, float]] = {
    "Mikhail Tal": {
        "tactical": 0.97,
        "positional": 0.25,
        "aggressive": 0.96,
        "solid": 0.18,
        "blitz_speed": 0.45,
        "low_draw": 0.85,
        "flank_openings": 0.35,
        "counterattack": 0.9,
    },
    "Anatoly Karpov": {
        "tactical": 0.35,
        "positional": 0.96,
        "aggressive": 0.3,
        "solid": 0.95,
        "blitz_speed": 0.25,
        "low_draw": 0.55,
        "flank_openings": 0.4,
        "counterattack": 0.45,
    },
    "Garry Kasparov": {
        "tactical": 0.9,
        "positional": 0.75,
        "aggressive": 0.88,
        "solid": 0.45,
        "blitz_speed": 0.55,
        "low_draw": 0.8,
        "flank_openings": 0.5,
        "counterattack": 0.85,
    },
    "Magnus Carlsen": {
        "tactical": 0.7,
        "positional": 0.88,
        "aggressive": 0.55,
        "solid": 0.8,
        "blitz_speed": 0.75,
        "low_draw": 0.7,
        "flank_openings": 0.55,
        "counterattack": 0.65,
    },
    "Bobby Fischer": {
        "tactical": 0.82,
        "positional": 0.78,
        "aggressive": 0.72,
        "solid": 0.7,
        "blitz_speed": 0.35,
        "low_draw": 0.75,
        "flank_openings": 0.45,
        "counterattack": 0.7,
    },
    "Tigran Petrosian": {
        "tactical": 0.4,
        "positional": 0.92,
        "aggressive": 0.2,
        "solid": 0.98,
        "blitz_speed": 0.2,
        "low_draw": 0.25,
        "flank_openings": 0.35,
        "counterattack": 0.55,
    },
    "Viswanathan Anand": {
        "tactical": 0.72,
        "positional": 0.7,
        "aggressive": 0.58,
        "solid": 0.62,
        "blitz_speed": 0.82,
        "low_draw": 0.65,
        "flank_openings": 0.5,
        "counterattack": 0.6,
    },
    "Ian Nepomniachtchi": {
        "tactical": 0.86,
        "positional": 0.55,
        "aggressive": 0.84,
        "solid": 0.4,
        "blitz_speed": 0.88,
        "low_draw": 0.82,
        "flank_openings": 0.45,
        "counterattack": 0.75,
    },
    "Baadur Jobava": {
        "tactical": 0.8,
        "positional": 0.45,
        "aggressive": 0.9,
        "solid": 0.35,
        "blitz_speed": 0.7,
        "low_draw": 0.78,
        "flank_openings": 0.7,
        "counterattack": 0.8,
    },
    "Richard Rapport": {
        "tactical": 0.75,
        "positional": 0.5,
        "aggressive": 0.82,
        "solid": 0.38,
        "blitz_speed": 0.65,
        "low_draw": 0.72,
        "flank_openings": 0.85,
        "counterattack": 0.7,
    },
}

GAMBIT_HINTS = (
    "gambit",
    "englund",
    "scandinavian",
    "vienna",
    "king's gambit",
    "kings gambit",
    "benko",
    "budapest",
    "latvian",
    "blackmar",
    "danish",
    "evans",
)


@dataclass
class Bucket:
    wins: int = 0
    losses: int = 0
    draws: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.draws

    def add(self, result: str) -> None:
        if result == "win":
            self.wins += 1
        elif result == "loss":
            self.losses += 1
        else:
            self.draws += 1

    def score_pct(self) -> float | None:
        if not self.games:
            return None
        return round(100 * (self.wins + 0.5 * self.draws) / self.games, 1)


def hour_label_ampm(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:00 {suffix}"


def tc_sort_key(value: str) -> tuple[int, int, str]:
    """Sort time controls: bullet → blitz → rapid → daily → unknown."""
    if value.startswith("1/"):
        try:
            return (900_000, 0, value)
        except ValueError:
            return (999_999, 0, value)
    if "+" in value:
        base, inc = value.split("+", 1)
        try:
            return (int(base), int(inc), value)
        except ValueError:
            return (999_999, 0, value)
    try:
        return (int(value), 0, value)
    except ValueError:
        return (999_999, 0, value)


def format_time_control_exact(value: str | None) -> tuple[str, str]:
    """Return (canonical_key, human label) e.g. ('300', '5 min') or ('180+2', '3+2')."""
    if not value or value == "-":
        return "unknown", "Unknown"

    if value.startswith("1/"):
        try:
            period = int(value.split("/", 1)[1])
            days = period / 86400
            if days >= 1:
                d = int(days) if days == int(days) else round(days, 1)
                label = f"Daily ({d} day{'s' if d != 1 else ''})"
            else:
                label = f"Daily ({value})"
            return value, label
        except ValueError:
            return value, "Daily"

    if "+" in value:
        base, inc = value.split("+", 1)
        try:
            base_s = int(base)
            inc_s = int(inc)
            if base_s % 60 == 0 and base_s >= 60:
                label = f"{base_s // 60}+{inc_s}"
            else:
                label = f"{base_s}s+{inc_s}"
            return value, label
        except ValueError:
            return value, value

    try:
        seconds = int(value)
    except ValueError:
        return value, value

    if seconds >= 86400:
        return value, f"Daily ({seconds // 86400} days)"
    if seconds >= 60 and seconds % 60 == 0:
        mins = seconds // 60
        return value, f"{mins} min"
    if seconds >= 60:
        return value, f"{seconds // 60}m {seconds % 60}s"
    return value, f"{seconds} sec"


def parse_time_control(value: str | None) -> tuple[str, str]:
    """Broad category (bullet/blitz/rapid/daily) for legacy groupings."""
    if not value or value == "-":
        return "unknown", "Unknown"
    if value.startswith("1/"):
        return "daily", format_time_control_exact(value)[1]
    if "+" in value:
        base, inc = value.split("+", 1)
        try:
            base_s = int(base)
        except ValueError:
            return "unknown", value
        if base_s >= 86400:
            return "daily", format_time_control_exact(value)[1]
        if base_s <= 180:
            return "bullet", format_time_control_exact(value)[1]
        if base_s <= 600:
            return "blitz", format_time_control_exact(value)[1]
        return "rapid", format_time_control_exact(value)[1]
    try:
        seconds = int(value)
    except ValueError:
        return "unknown", value
    if seconds >= 86400:
        return "daily", format_time_control_exact(value)[1]
    if seconds <= 180:
        return "bullet", format_time_control_exact(value)[1]
    if seconds <= 600:
        return "blitz", format_time_control_exact(value)[1]
    if seconds <= 1800:
        return "rapid", format_time_control_exact(value)[1]
    return "daily", format_time_control_exact(value)[1]


def opening_name(headers: dict[str, str]) -> str:
    eco = headers.get("ECO", "Unknown")
    url = headers.get("ECOUrl", "")
    if url:
        slug = url.rstrip("/").split("/")[-1]
        name = re.sub(r"-+", " ", slug).strip()
        if name:
            return f"{eco} — {name[:80]}"
    return eco or "Unknown"


def eco_family(eco: str) -> str:
    if not eco or eco == "Unknown":
        return "Unknown"
    return eco[0]


def player_result(headers: dict[str, str], username: str) -> tuple[str, str, int | None, int | None]:
    white = headers.get("White", "")
    black = headers.get("Black", "")
    result = headers.get("Result", "*")
    if white.lower() == username.lower():
        color = "white"
        my_elo = int(headers["WhiteElo"]) if headers.get("WhiteElo", "").isdigit() else None
        opp_elo = int(headers["BlackElo"]) if headers.get("BlackElo", "").isdigit() else None
        if result == "1-0":
            outcome = "win"
        elif result == "0-1":
            outcome = "loss"
        elif result == "1/2-1/2":
            outcome = "draw"
        else:
            outcome = "other"
    elif black.lower() == username.lower():
        color = "black"
        my_elo = int(headers["BlackElo"]) if headers.get("BlackElo", "").isdigit() else None
        opp_elo = int(headers["WhiteElo"]) if headers.get("WhiteElo", "").isdigit() else None
        if result == "0-1":
            outcome = "win"
        elif result == "1-0":
            outcome = "loss"
        elif result == "1/2-1/2":
            outcome = "draw"
        else:
            outcome = "other"
    else:
        return "skip", "skip", None, None
    return color, outcome, my_elo, opp_elo


def parse_local_hour(headers: dict[str, str]) -> int | None:
    raw = headers.get("StartTime") or headers.get("UTCTime")
    date_raw = headers.get("UTCDate") or headers.get("Date")
    if not raw or not date_raw:
        return None
    try:
        dt_utc = datetime.strptime(f"{date_raw} {raw}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(TZ).hour
    except ValueError:
        return None


def parse_local_weekday(headers: dict[str, str]) -> str | None:
    date_raw = headers.get("UTCDate") or headers.get("Date")
    if not date_raw:
        return None
    try:
        dt = datetime.strptime(date_raw, "%Y.%m.%d")
        return dt.strftime("%A")
    except ValueError:
        return None


def parse_month(headers: dict[str, str]) -> str | None:
    date_raw = headers.get("UTCDate") or headers.get("Date")
    if not date_raw:
        return None
    return date_raw[:7]


def termination_kind(term: str, username: str) -> str:
    t = term.lower()
    if "timeout" in t or "time forfeit" in t:
        return "timeout"
    if "checkmate" in t:
        return "checkmate"
    if "resign" in t:
        return "resignation"
    if "abandon" in t:
        return "abandoned"
    if "repetition" in t or "stalemate" in t or "agreement" in t or "insufficient" in t:
        return "drawish"
    if username.lower() in t and "won" in t:
        return "other_win"
    return "other_loss"


def bucket_to_dict(bucket: Bucket) -> dict[str, Any]:
    return {
        "games": bucket.games,
        "wins": bucket.wins,
        "losses": bucket.losses,
        "draws": bucket.draws,
        "score_pct": bucket.score_pct(),
    }


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def is_gambit_opening(opening: str) -> bool:
    low = opening.lower()
    return any(hint in low for hint in GAMBIT_HINTS)


def compute_best_time_control(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible_high = [r for r in rows if r["games"] >= MIN_TC_GAMES_HIGH and r["score_pct"] is not None]
    eligible_med = [r for r in rows if r["games"] >= MIN_TC_GAMES_MEDIUM and r["score_pct"] is not None]
    pool = eligible_high or eligible_med or [r for r in rows if r["games"] >= 20 and r["score_pct"] is not None]

    if not pool:
        return {
            "label": None,
            "key": None,
            "score_pct": None,
            "games": 0,
            "confidence": "low",
            "summary_ro": "Nu există încă destule partide pe un singur format pentru o concluzie clară.",
        }

    best = max(pool, key=lambda r: (r["score_pct"], r["games"]))
    confidence = "high" if best["games"] >= MIN_TC_GAMES_HIGH else "medium" if best["games"] >= MIN_TC_GAMES_MEDIUM else "low"

    # Highlight a faster/slower alternative if clearly different and enough games.
    alt_note = ""
    others = [r for r in pool if r["key"] != best["key"]]
    if others:
        main_played = max(pool, key=lambda r: r["games"])
        if main_played["key"] != best["key"] and main_played["games"] >= MIN_TC_GAMES_HIGH:
            alt_note = (
                f" Joci cel mai mult la {main_played['label']} ({main_played['games']:,} partide, "
                f"{main_played['score_pct']}%), dar procentul tău e mai bun la {best['label']}."
            )

    conf_ro = {"high": "încredere mare", "medium": "încredere medie", "low": "eșantion mic"}.get(confidence, "")
    summary_ro = (
        f"Cel mai bun time control pentru tine: **{best['label']}** — "
        f"**{best['score_pct']}%** din **{best['games']:,}** partide ({conf_ro}).{alt_note}"
    )

    small_sample_star = None
    spicy = [r for r in rows if 15 <= r["games"] < MIN_TC_GAMES_MEDIUM and r["score_pct"] is not None]
    if spicy:
        star = max(spicy, key=lambda r: (r["score_pct"], r["games"]))
        if star["score_pct"] > (best["score_pct"] or 0) + 2:
            small_sample_star = {
                "label": star["label"],
                "score_pct": star["score_pct"],
                "games": star["games"],
                "note_ro": f"La {star['label']} ai {star['score_pct']}% ({star['games']} partide) — promițător, dar încă puține jocuri.",
            }

    return {
        "label": best["label"],
        "key": best["key"],
        "score_pct": best["score_pct"],
        "games": best["games"],
        "confidence": confidence,
        "summary_ro": summary_ro.replace("**", ""),
        "small_sample_highlight": small_sample_star,
    }


def eco_share(by_eco_family: list[dict[str, Any]]) -> dict[str, float]:
    total = sum(r["games"] for r in by_eco_family) or 1
    return {r["key"]: r["games"] / total for r in by_eco_family}


def compute_style_profile(ctx: dict[str, Any], gambit_games: int, unique_openings: int) -> dict[str, Any]:
    overall = ctx["overall"]
    games = overall["games"] or 1
    draw_rate = overall["draws"] / games
    term = ctx["terminations"]
    checkmate_games = term.get("checkmate", {}).get("games", 0)
    resignation_wins = term.get("resignation", {}).get("wins", 0)
    short_loss_rate = ctx["short_losses"] / max(overall["losses"], 1)
    gambit_rate = gambit_games / games

    eco = eco_share(ctx["by_eco_family"])
    flank_share = eco.get("A", 0.0)
    semi_open = eco.get("B", 0.0)
    open_classical = eco.get("C", 0.0) + eco.get("D", 0.0)
    indian = eco.get("E", 0.0)

    avg_win = ctx["game_length"]["avg_moves_win"] or 60
    avg_loss = ctx["game_length"]["avg_moves_loss"] or 60
    avg_len = (avg_win + avg_loss) / 2

    blitz_games = sum(r["games"] for r in ctx["by_time_class"] if r["key"] in {"bullet", "blitz"})
    blitz_share = blitz_games / games

    white = ctx["by_color"]["white"]["score_pct"] or 50
    black = ctx["by_color"]["black"]["score_pct"] or 50
    white_edge = max(0.0, white - black) / 20

    vs_higher = ctx["vs_rating"]["vs_higher"]["score_pct"] or 0

    traits = {
        "tactical": clamp01(
            0.35 * (checkmate_games / games)
            + 0.25 * (1 - draw_rate / 0.12)
            + 0.2 * short_loss_rate * 2
            + 0.2 * gambit_rate * 4
        ),
        "positional": clamp01(
            0.4 * (avg_len / 85)
            + 0.35 * open_classical
            + 0.25 * (resignation_wins / max(overall["wins"], 1))
        ),
        "aggressive": clamp01(
            0.35 * gambit_rate * 5
            + 0.3 * (1 - draw_rate / 0.1)
            + 0.2 * semi_open
            + 0.15 * short_loss_rate
        ),
        "solid": clamp01(
            0.4 * draw_rate / 0.08
            + 0.35 * (avg_loss / 90)
            + 0.25 * (1 - short_loss_rate * 3)
        ),
        "blitz_speed": clamp01(0.7 * blitz_share + 0.3 * (1 - avg_len / 80)),
        "low_draw": clamp01(1 - draw_rate / 0.1),
        "flank_openings": clamp01(0.65 * flank_share + 0.35 * indian),
        "counterattack": clamp01(0.4 * semi_open + 0.35 * gambit_rate * 4 + 0.25 * (black / 100)),
    }

    # Human-readable style tags (top traits).
    ranked = sorted(traits.items(), key=lambda item: item[1], reverse=True)
    top = [name for name, score in ranked[:3] if score >= 0.55]

    label_map = {
        "tactical": "tactic",
        "positional": "pozițional",
        "aggressive": "agresiv",
        "solid": "solid",
        "blitz_speed": "blitz-rapid",
        "low_draw": "decisiv (puține remize)",
        "flank_openings": "deschideri de flanc",
        "counterattack": "contrajoc",
    }
    tags_ro = [label_map.get(t, t) for t in top]

    if traits["aggressive"] >= 0.6 and traits["blitz_speed"] >= 0.65:
        archetype_ro = "Luptător de blitz — intri în complicații și cauți decisivitate."
    elif traits["positional"] >= 0.6 and traits["solid"] >= 0.55:
        archetype_ro = "Jucător pozițional — preferi structuri clare și presiune lentă."
    elif traits["tactical"] >= 0.65:
        archetype_ro = "Jucător tactic — pozițiile se decid prin calcul și inițiativă."
    elif traits["flank_openings"] >= 0.55:
        archetype_ro = "Jucător de sistem — îți place să eviți linii teoretice mainstream."
    else:
        archetype_ro = "Jucător practic — îți alegi partidele decisive, fără un stil rigid."

    narrative_ro = (
        f"Stilul tău online e în primul rând **{', '.join(tags_ro) or 'echilibrat'}**. "
        f"Lungime medie ~{avg_len:.0f} mutări; remize doar {draw_rate * 100:.1f}%; "
        f"gambituri/deschideri sharp în ~{gambit_rate * 100:.1f}% din partide. "
        f"Ca alb ești mai puternic ({white}% vs {black}% negru). "
        f"Împotriva jucătorilor mai tari ai {vs_higher}% — tipic pentru cine urcă prin volume, nu prin farmec în fiecare partidă."
    ).replace("**", "")

    return {
        "traits": {k: round(v * 100, 1) for k, v in traits.items()},
        "tags_ro": tags_ro,
        "archetype_ro": archetype_ro,
        "narrative_ro": narrative_ro,
        "stats": {
            "draw_rate_pct": round(draw_rate * 100, 1),
            "gambit_rate_pct": round(gambit_rate * 100, 1),
            "avg_moves": round(avg_len, 1),
            "unique_openings": unique_openings,
            "blitz_share_pct": round(blitz_share * 100, 1),
        },
    }


def compare_with_legends(traits: dict[str, float]) -> list[dict[str, Any]]:
    user = {k: v / 100 for k, v in traits.items()}
    dims = list(next(iter(LEGEND_ARCHETYPES.values())).keys())
    results: list[dict[str, Any]] = []

    for name, archetype in LEGEND_ARCHETYPES.items():
        dist = sum((user.get(d, 0) - archetype.get(d, 0)) ** 2 for d in dims)
        similarity = round(100 * max(0.0, 1 - (dist / len(dims)) ** 0.5), 1)
        overlap = [d for d in dims if abs(user.get(d, 0) - archetype.get(d, 0)) <= 0.22]
        results.append(
            {
                "name": name,
                "similarity_pct": similarity,
                "overlap_traits": overlap[:4],
            }
        )

    results.sort(key=lambda r: (-r["similarity_pct"], r["name"]))
    blurbs = {
        "Mikhail Tal": "Complicații, sacrificii, partide care se decid pe calcul pur.",
        "Anatoly Karpov": "Presiune pozițională, joc solid, exploatează micile avantaje.",
        "Garry Kasparov": "Agresivitate activă, pregătire de deschidere, luptă pe toată tabla.",
        "Magnus Carlsen": "Practic, endgame puternic, răbdare și conversie în avantaje mici.",
        "Bobby Fischer": "Precizie, claritate, maxim din fiecare inițiativă.",
        "Tigran Petrosian": "Prophylaxis, soliditate, întoarce pericolele împotriva adversarului.",
        "Viswanathan Anand": "Rapid, flexibil, excelent la viteză și decizii practice.",
        "Ian Nepomniachtchi": "Blitz/tactic, ritm mare, deschideri dinamice.",
        "Baadur Jobava": "Sisteme neconvenționale, joc de atac, surprinde repertoarul.",
        "Richard Rapport": "Flancuri, structuri atipice, chess creativ și imprevizibil.",
    }
    for row in results:
        row["blurb_ro"] = blurbs.get(row["name"], "")
    return results


def legend_archetypes_for_ui() -> dict[str, Any]:
    dims = list(next(iter(LEGEND_ARCHETYPES.values())).keys())
    return {
        "dimensions": dims,
        "dimension_labels_ro": {
            "tactical": "Tactic",
            "positional": "Pozițional",
            "aggressive": "Agresiv",
            "solid": "Solid",
            "blitz_speed": "Viteză blitz",
            "low_draw": "Decisiv",
            "flank_openings": "Flancuri",
            "counterattack": "Contrajoc",
        },
        "players": {
            name: {k: round(v * 100, 1) for k, v in traits.items()}
            for name, traits in LEGEND_ARCHETYPES.items()
        },
        "radar_compare": ["Viswanathan Anand", "Magnus Carlsen", "Mikhail Tal"],
    }


def build_hour_weekday_heatmap(by_hour_weekday: dict[tuple[str, int], Bucket]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for weekday in WEEKDAY_ORDER:
        for hour in range(24):
            bucket = by_hour_weekday.get((weekday, hour), Bucket())
            if bucket.games < 1:
                continue
            cells.append(
                {
                    "weekday": weekday,
                    "weekday_short": weekday[:3],
                    "hour": hour,
                    "hour_label": hour_label_ampm(hour),
                    "games": bucket.games,
                    "score_pct": bucket.score_pct(),
                    "reliable": bucket.games >= MIN_HEATMAP_GAMES,
                }
            )

    reliable = [c for c in cells if c["reliable"] and c["score_pct"] is not None]
    best = max(reliable, key=lambda c: (c["score_pct"], c["games"])) if reliable else None
    worst = min(reliable, key=lambda c: (c["score_pct"], -c["games"])) if reliable else None

    return {
        "weekdays": list(WEEKDAY_ORDER),
        "hours": list(range(24)),
        "min_games_reliable": MIN_HEATMAP_GAMES,
        "cells": cells,
        "best": best,
        "worst": worst,
    }


def sorted_buckets(data: dict[str, Bucket], min_games: int = 1) -> list[dict[str, Any]]:
    rows = []
    for key, bucket in data.items():
        if bucket.games < min_games:
            continue
        rows.append({"key": key, **bucket_to_dict(bucket)})
    rows.sort(key=lambda r: (-(r["score_pct"] or 0), -r["games"]))
    return rows


def build_insights(ctx: dict[str, Any]) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []
    overall = ctx["overall"]["score_pct"]

    color = ctx["by_color"]
    if color["white"]["games"] > 100 and color["black"]["games"] > 100:
        w, b = color["white"]["score_pct"], color["black"]["score_pct"]
        if w - b >= 4:
            insights.append(
                {
                    "type": "weakness",
                    "title": "Black repertoire underperforms",
                    "detail": f"You score {w}% as White but only {b}% as Black ({w - b:.1f} pp gap). Prioritize black openings and defensive structures you already play often.",
                }
            )
        elif b - w >= 4:
            insights.append(
                {
                    "type": "weakness",
                    "title": "White repertoire underperforms",
                    "detail": f"You score {b}% as Black but only {w}% as White. Review early white systems where you bleed points quickly.",
                }
            )

    worst_openings = ctx["weak_openings"][:3]
    if worst_openings:
        names = ", ".join(o["key"] for o in worst_openings)
        insights.append(
            {
                "type": "weakness",
                "title": "Opening leaks costing rating",
                "detail": f"Lowest-performing frequent openings: {names}. Study model games, blunder-check the first 10 moves, or drop lines with repeated fast losses.",
            }
        )

    best_openings = ctx["strong_openings"][:3]
    if best_openings:
        names = ", ".join(o["key"] for o in best_openings)
        insights.append(
            {
                "type": "strength",
                "title": "Reliable opening weapons",
                "detail": f"You perform well in: {names}. Double down on these systems and transpose into them when possible.",
            }
        )

    best_tc = ctx.get("best_time_control")
    if best_tc and best_tc.get("label"):
        insights.insert(
            0,
            {
                "type": "strength",
                "title": f"Cel mai bun format: {best_tc['label']}",
                "detail": best_tc["summary_ro"],
            },
        )

    style = ctx.get("playing_style")
    if style:
        insights.insert(
            1 if best_tc and best_tc.get("label") else 0,
            {
                "type": "improvement",
                "title": "Profil de stil",
                "detail": f"{style['archetype_ro']} {style['narrative_ro']}",
            },
        )

    legends = ctx.get("legend_matches") or []
    if legends:
        top = legends[0]
        runner = legends[1] if len(legends) > 1 else None
        detail = (
            f"Cel mai apropiat de stilul tău online: {top['name']} ({top['similarity_pct']}% potrivire). "
            f"{top['blurb_ro']}"
        )
        if runner:
            detail += f" Apoi {runner['name']} ({runner['similarity_pct']}%)."
        insights.insert(
            2 if best_tc and best_tc.get("label") else 1,
            {
                "type": "strength",
                "title": "Comparație cu marii jucători",
                "detail": detail,
            },
        )

    hours = ctx["by_hour"]
    if hours["best"]:
        h = hours["best"]
        insights.append(
            {
                "type": "strength",
                "title": "Peak performance window",
                "detail": f"Your best hour is {h['label']} ({h['score_pct']}% over {h['games']} games). Schedule important games or study reviews around this window.",
            }
        )
    if hours["worst"]:
        h = hours["worst"]
        insights.append(
            {
                "type": "weakness",
                "title": "Tilt-prone playing window",
                "detail": f"You score only {h['score_pct']}% at {h['label']} ({h['games']} games). Avoid ranked sessions then, or play slower controls with pre-game tactics warmup.",
            }
        )

    term = ctx["terminations"]
    timeout_losses = term.get("timeout", {}).get("losses", 0)
    if timeout_losses >= 25:
        insights.append(
            {
                "type": "weakness",
                "title": "Clock management",
                "detail": f"You lost {timeout_losses} games on time. Practice 15-second decision discipline and add increment formats where you convert winning positions more often.",
            }
        )

    short_losses = ctx["short_losses"]
    if short_losses >= 50:
        insights.append(
            {
                "type": "weakness",
                "title": "Early collapses",
                "detail": f"{short_losses} losses ended in under 20 moves. Run opening blunder checks and maintain a small, well-studied repertoire instead of experimenting live.",
            }
        )

    vs_lower = ctx["vs_rating"]["vs_lower"]
    if vs_lower["games"] >= 100 and (vs_lower["score_pct"] or 0) < overall - 5:
        insights.append(
            {
                "type": "weakness",
                "title": "Dropping points to lower-rated opponents",
                "detail": f"You score {vs_lower['score_pct']}% vs lower-rated players (overall {overall}%). Convert advantages cleanly and avoid unnecessary complications.",
            }
        )

    vs_higher = ctx["vs_rating"]["vs_higher"]
    if vs_higher["games"] >= 100 and (vs_higher["score_pct"] or 0) >= overall + 3:
        insights.append(
            {
                "type": "strength",
                "title": "Giant-killer tendency",
                "detail": f"You score {vs_higher['score_pct']}% vs higher-rated opponents. You rise in tough fights — use that confidence but don't chase ratings with reckless gambits.",
            }
        )

    time_controls = [r for r in ctx["by_time_control"] if r["games"] >= 20]
    if len(time_controls) >= 2:
        weakest = min(time_controls, key=lambda x: (x["score_pct"] or 0, -x["games"]))
        strongest = max(time_controls, key=lambda x: (x["score_pct"] or 0, x["games"]))
        if (strongest["score_pct"] or 0) - (weakest["score_pct"] or 0) >= 5:
            insights.append(
                {
                    "type": "improvement",
                    "title": "Format focus",
                    "detail": f"Strongest pool: {strongest['label']} ({strongest['score_pct']}% over {strongest['games']} games). Weakest: {weakest['label']} ({weakest['score_pct']}% over {weakest['games']} games). Train at the speed of your weakest pool.",
                }
            )

    if not insights:
        insights.append(
            {
                "type": "improvement",
                "title": "Keep building patterns",
                "detail": "No single glaring leak detected. Next gains likely come from deeper opening prep and reviewing losses from your worst hour.",
            }
        )

    return insights


def analyze(pgn_path: Path, username: str) -> dict[str, Any]:
    overall = Bucket()
    by_color: dict[str, Bucket] = defaultdict(Bucket)
    by_time_class: dict[str, Bucket] = defaultdict(Bucket)
    by_time_control: dict[str, Bucket] = defaultdict(Bucket)
    tc_labels: dict[str, str] = {}
    by_hour: dict[int, Bucket] = defaultdict(Bucket)
    by_weekday: dict[str, Bucket] = defaultdict(Bucket)
    by_hour_weekday: dict[tuple[str, int], Bucket] = defaultdict(Bucket)
    by_month: dict[str, Bucket] = defaultdict(Bucket)
    by_opening: dict[str, Bucket] = defaultdict(Bucket)
    by_eco_family: dict[str, Bucket] = defaultdict(Bucket)
    term_outcome: dict[str, Bucket] = defaultdict(Bucket)
    vs_lower = Bucket()
    vs_equal = Bucket()
    vs_higher = Bucket()
    ratings: list[int] = []
    rating_by_month: dict[str, list[int]] = defaultdict(list)
    move_counts_win: list[int] = []
    move_counts_loss: list[int] = []
    short_losses = 0
    gambit_games = 0
    unique_openings: set[str] = set()
    links: list[str] = []

    text = pgn_path.read_text(encoding="utf-8", errors="replace")
    stream = io.StringIO(text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        headers = dict(game.headers)
        color, outcome, my_elo, opp_elo = player_result(headers, username)
        if outcome == "skip" or outcome == "other":
            continue

        overall.add(outcome)
        by_color[color].add(outcome)

        tc_raw = headers.get("TimeControl")
        tc_class, _ = parse_time_control(tc_raw)
        by_time_class[tc_class].add(outcome)
        tc_key, tc_exact_label = format_time_control_exact(tc_raw)
        tc_labels[tc_key] = tc_exact_label
        by_time_control[tc_key].add(outcome)

        hour = parse_local_hour(headers)
        weekday = parse_local_weekday(headers)
        if hour is not None:
            by_hour[hour].add(outcome)
        if weekday:
            by_weekday[weekday].add(outcome)
        if hour is not None and weekday:
            by_hour_weekday[(weekday, hour)].add(outcome)

        month = parse_month(headers)
        if month:
            by_month[month].add(outcome)

        opening = opening_name(headers)
        by_opening[opening].add(outcome)
        unique_openings.add(opening)
        if is_gambit_opening(opening):
            gambit_games += 1
        by_eco_family[eco_family(headers.get("ECO", ""))].add(outcome)

        term = termination_kind(headers.get("Termination", ""), username)
        term_outcome[term].add(outcome)

        if my_elo is not None:
            ratings.append(my_elo)
            if month:
                rating_by_month[month].append(my_elo)

        if my_elo is not None and opp_elo is not None:
            diff = opp_elo - my_elo
            if diff >= 50:
                vs_higher.add(outcome)
            elif diff <= -50:
                vs_lower.add(outcome)
            else:
                vs_equal.add(outcome)

        moves = sum(1 for _ in game.mainline_moves())
        if outcome == "win":
            move_counts_win.append(moves)
        elif outcome == "loss":
            move_counts_loss.append(moves)
            if moves < 20:
                short_losses += 1

        if link := headers.get("Link"):
            links.append(link)

    hour_rows = []
    for hour in range(24):
        bucket = by_hour.get(hour, Bucket())
        if bucket.games < MIN_HOUR_GAMES:
            continue
        hour_rows.append(
            {
                "hour": hour,
                "label": hour_label_ampm(hour),
                "games": bucket.games,
                "score_pct": bucket.score_pct(),
            }
        )
    hour_rows.sort(key=lambda r: (-(r["score_pct"] or 0), -r["games"]))

    opening_rows = sorted_buckets(by_opening, MIN_OPENING_GAMES)
    weak_openings = sorted(opening_rows, key=lambda r: (r["score_pct"] or 0, -r["games"]))[:8]
    strong_openings = sorted(opening_rows, key=lambda r: (-(r["score_pct"] or 0), -r["games"]))[:8]

    rating_trend = [
        {"month": month, "avg_rating": round(mean(vals), 1), "games": by_month[month].games}
        for month, vals in sorted(rating_by_month.items())
        if vals
    ]

    ctx = {
        "meta": {
            "username": username,
            "timezone": "Europe/Bucharest",
            "source_pgn": str(pgn_path),
            "generated_at": datetime.now(TZ).isoformat(),
            "total_games": overall.games,
        },
        "overall": bucket_to_dict(overall),
        "ratings": {
            "current_estimate": ratings[-1] if ratings else None,
            "peak": max(ratings) if ratings else None,
            "average": round(mean(ratings), 1) if ratings else None,
        },
        "by_color": {k: bucket_to_dict(v) for k, v in by_color.items()},
        "by_time_class": sorted_buckets(by_time_class, 1),
        "by_time_control": [
            {
                "key": key,
                "label": tc_labels.get(key, key),
                **bucket_to_dict(bucket),
            }
            for key, bucket in sorted(by_time_control.items(), key=lambda item: tc_sort_key(item[0]))
            if bucket.games >= 1
        ],
        "by_weekday": sorted_buckets(by_weekday, 1),
        "by_eco_family": sorted_buckets(by_eco_family, 1),
        "by_hour": {
            "all": hour_rows,
            "best": hour_rows[0] if hour_rows else None,
            "worst": hour_rows[-1] if hour_rows else None,
        },
        "by_hour_weekday": build_hour_weekday_heatmap(by_hour_weekday),
        "rating_trend": rating_trend,
        "openings": opening_rows[:25],
        "weak_openings": weak_openings,
        "strong_openings": strong_openings,
        "terminations": {k: bucket_to_dict(v) for k, v in sorted(term_outcome.items())},
        "vs_rating": {
            "vs_lower": bucket_to_dict(vs_lower),
            "vs_equal": bucket_to_dict(vs_equal),
            "vs_higher": bucket_to_dict(vs_higher),
        },
        "game_length": {
            "avg_moves_win": round(mean(move_counts_win), 1) if move_counts_win else None,
            "avg_moves_loss": round(mean(move_counts_loss), 1) if move_counts_loss else None,
        },
        "short_losses": short_losses,
    }
    ctx["best_time_control"] = compute_best_time_control(ctx["by_time_control"])
    ctx["playing_style"] = compute_style_profile(ctx, gambit_games, len(unique_openings))
    ctx["legend_matches"] = compare_with_legends(ctx["playing_style"]["traits"])
    ctx["legend_archetypes"] = legend_archetypes_for_ui()
    ctx["insights"] = build_insights(ctx)
    return ctx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pgn",
        type=Path,
        default=Path.home() / "Downloads" / "chesscom-netrebnic-all-games.pgn",
    )
    parser.add_argument("--username", default=USERNAME)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "stats.json",
    )
    args = parser.parse_args()

    stats = analyze(args.pgn, args.username)
    stats_json = json.dumps(stats, indent=2)
    args.out.write_text(stats_json, encoding="utf-8")

    template = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
    dashboard = template.replace("__STATS_JSON__", json.dumps(stats))
    dashboard_path = args.out.parent / "dashboard.html"
    dashboard_path.write_text(dashboard, encoding="utf-8")

    print(json.dumps(
        {
            "games": stats["meta"]["total_games"],
            "score_pct": stats["overall"]["score_pct"],
            "best_hour": stats["by_hour"]["best"],
            "stats_json": str(args.out),
            "dashboard": str(dashboard_path),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()