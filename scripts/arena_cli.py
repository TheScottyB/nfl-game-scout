#!/usr/bin/env python3
"""CLI helper to validate NFL Game Scout slates, generate multi-model prompts, ingest predictions, and grade games."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA_DIR = REPO_ROOT / "docs" / "data"

SAFE_ID_RE = re.compile(r"^[a-z0-9-]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SUPPORTED_PROVIDERS = {"gemini", "claude-cli", "anthropic", "openai"}

RUBRIC_CAPS = {
    "winner": 20,
    "scoreMargin": 20,
    "spreadTotal": 15,
    "winScriptRef": 15,
    "orgSentiment": 15,
    "surpriseBonus": 15,
}

REQUIRED_GAME_KEYS = {
    "id",
    "label",
    "away",
    "home",
    "kickoffUtc",
    "broadcast",
    "venue",
    "odds",
    "weather",
    "injuries",
    "referee",
    "winScripts",
    "sentiment30d",
    "orgChart",
    "predictions",
}


def _is_nonneg_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val >= 0


def _is_https_url(val: Any) -> bool:
    if not isinstance(val, str) or not val.startswith("https://"):
        return False
    parsed = urllib.parse.urlparse(val)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _parse_iso8601(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}: expected non-empty ISO-8601 timestamp, got {value!r}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{context}: invalid ISO-8601 timestamp {value!r}") from exc


def _resolve_slate_path(slate_id: str, data_dir: Path = DOCS_DATA_DIR) -> Path:
    if not isinstance(slate_id, str) or not SAFE_ID_RE.fullmatch(slate_id):
        raise ValueError(f"Unsafe or invalid slate ID: {slate_id!r} (expected ^[a-z0-9-]+$)")
    resolved_dir = data_dir.resolve()
    candidate = (resolved_dir / f"{slate_id}.json").resolve()
    if candidate.parent != resolved_dir:
        raise ValueError(f"Slate path escapes data directory: {candidate}")
    return candidate


def _atomic_write_json(target_path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.stem}_",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def validate_slate_dict(slate: dict[str, Any], source_name: str = "<slate>") -> list[str]:
    errors: list[str] = []
    try:
        _parse_iso8601(slate.get("updatedAtUtc", ""), f"{source_name}.updatedAtUtc")
    except ValueError as exc:
        errors.append(str(exc))

    slate_short = slate.get("studioShortUrl")
    if slate_short is not None and not _is_https_url(slate_short):
        errors.append(f"{source_name}.studioShortUrl: must be https:// URL, got {slate_short!r}")

    missed_games = slate.get("missedGames")
    if missed_games is not None:
        if not isinstance(missed_games, list):
            errors.append(f"{source_name}.missedGames: must be a list")
        else:
            for idx, mg in enumerate(missed_games):
                if not isinstance(mg, dict):
                    errors.append(f"{source_name}.missedGames[{idx}]: must be an object")
                    continue
                mg_id = mg.get("id", "<unknown>")
                if not isinstance(mg_id, str) or not SAFE_ID_RE.fullmatch(mg_id):
                    errors.append(f"{source_name}.missedGames[{idx}]: id must match ^[a-z0-9-]+$")
                for req_k in ("label", "matchup", "venue", "winnerAbbr", "reasonMissed", "recapSummary"):
                    if not isinstance(mg.get(req_k), str) or not mg[req_k].strip():
                        errors.append(f"{source_name}.missedGames[{mg_id}].{req_k}: must be non-empty string")
                try:
                    _parse_iso8601(mg.get("kickoffUtc", ""), f"{source_name}.missedGames[{mg_id}].kickoffUtc")
                except ValueError as exc:
                    errors.append(str(exc))
                fs = mg.get("finalScore")
                if not isinstance(fs, dict) or not _is_nonneg_int(fs.get("away")) or not _is_nonneg_int(fs.get("home")):
                    errors.append(
                        f"{source_name}.missedGames[{mg_id}].finalScore: expected non-negative integer 'away' and 'home'"
                    )

    games = slate.get("games")
    if not isinstance(games, list) or not games:
        errors.append(f"{source_name}: 'games' must be a non-empty list")
        return errors

    for game in games:
        gid = game.get("id", "<unknown>")
        missing = REQUIRED_GAME_KEYS - set(game.keys())
        if missing:
            errors.append(f"{source_name}:{gid}: missing game keys {sorted(missing)}")
            continue

        if not isinstance(gid, str) or not SAFE_ID_RE.fullmatch(gid):
            errors.append(f"{source_name}:{gid}: game id must match ^[a-z0-9-]+$")

        game_short = game.get("studioShortUrl")
        if game_short is not None and not _is_https_url(game_short):
            errors.append(f"{gid}.studioShortUrl: must be https:// URL, got {game_short!r}")

        for side in ("away", "home"):
            color = game[side].get("color")
            if not isinstance(color, str) or not HEX_COLOR_RE.fullmatch(color):
                errors.append(f"{gid}.{side}.color: invalid hex color {color!r}")

        try:
            _parse_iso8601(game["kickoffUtc"], f"{gid}.kickoffUtc")
        except ValueError as exc:
            errors.append(str(exc))

        sent = game["sentiment30d"]
        for key in ("awayScore", "homeScore"):
            score = sent.get(key)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not (-5 <= score <= 5):
                errors.append(f"{gid}.sentiment30d.{key}: {score!r} must be between -5 and 5")

        tiers = game["orgChart"].get("tiers", [])
        if len(tiers) != 4:
            errors.append(f"{gid}.orgChart.tiers: expected 4 tiers, found {len(tiers)}")

        div_ctx = game.get("divisionContext")
        if div_ctx is not None:
            if not isinstance(div_ctx, dict) or not isinstance(div_ctx.get("isDivisional"), bool):
                errors.append(f"{gid}.divisionContext: must be an object with boolean 'isDivisional'")
            else:
                for opt_k in ("division", "awayDivRecord", "homeDivRecord", "note"):
                    if opt_k in div_ctx and div_ctx[opt_k] is not None and not isinstance(div_ctx[opt_k], str):
                        errors.append(f"{gid}.divisionContext.{opt_k}: must be a string")

        preds = game.get("predictions", [])
        if not preds:
            errors.append(f"{gid}: expected at least 1 prediction entry")
        for pred in preds:
            status = pred.get("status")
            model_id = pred.get("modelId", "<unknown>")
            if not isinstance(model_id, str) or not SAFE_ID_RE.fullmatch(model_id):
                errors.append(f"{gid}.predictions[{model_id}]: modelId must match ^[a-z0-9-]+$")
            if status not in {"locked", "pending", "graded"}:
                errors.append(f"{gid}.predictions[{model_id}]: invalid status {status!r}")

            meta = pred.get("modelMeta")
            if meta is not None:
                if not isinstance(meta, dict):
                    errors.append(f"{gid}.predictions[{model_id}].modelMeta: must be an object")
                else:
                    for req_mk in ("provider", "exactModelId", "harness"):
                        val = meta.get(req_mk)
                        if not isinstance(val, str) or not val.strip():
                            errors.append(
                                f"{gid}.predictions[{model_id}].modelMeta.{req_mk}: must be non-empty string"
                            )
                    if "runAtUtc" in meta and meta["runAtUtc"] is not None:
                        try:
                            _parse_iso8601(meta["runAtUtc"], f"{gid}.{model_id}.modelMeta.runAtUtc")
                        except ValueError as exc:
                            errors.append(str(exc))
                    if "latencySec" in meta and meta["latencySec"] is not None:
                        lat = meta["latencySec"]
                        if isinstance(lat, bool) or not isinstance(lat, (int, float)) or lat < 0:
                            errors.append(f"{gid}.predictions[{model_id}].modelMeta.latencySec: must be >= 0")
                    if "promptSha256" in meta and meta["promptSha256"] is not None:
                        sha = meta["promptSha256"]
                        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
                            errors.append(
                                f"{gid}.predictions[{model_id}].modelMeta.promptSha256: must be 64-char hex"
                            )
                    if "usage" in meta and meta["usage"] is not None and not isinstance(meta["usage"], dict):
                        errors.append(f"{gid}.predictions[{model_id}].modelMeta.usage: must be an object")

            receipt = pred.get("receiptUrl")
            if receipt is not None and not _is_https_url(receipt):
                errors.append(f"{gid}.predictions[{model_id}].receiptUrl: must be https:// URL, got {receipt!r}")

            if status in {"locked", "graded"}:
                try:
                    _parse_iso8601(pred.get("publishedAtUtc", ""), f"{gid}.{model_id}.publishedAtUtc")
                except ValueError as exc:
                    errors.append(str(exc))
                sc = pred.get("surpriseCall") or {}
                if not sc.get("title") or not sc.get("detail"):
                    errors.append(f"{gid}.predictions[{model_id}]: locked/graded entry missing surpriseCall title/detail")
                proj = pred.get("projectedScore")
                if (
                    not isinstance(proj, dict)
                    or not _is_nonneg_int(proj.get("away"))
                    or not _is_nonneg_int(proj.get("home"))
                    or not pred.get("winnerAbbr")
                ):
                    errors.append(
                        f"{gid}.predictions[{model_id}]: locked/graded entry requires winnerAbbr and non-negative integer projectedScore"
                    )

            grade = pred.get("grade")
            if grade is not None:
                if not isinstance(grade, dict):
                    errors.append(f"{gid}.predictions[{model_id}].grade: must be an object")
                else:
                    total = grade.get("total")
                    max_pts = grade.get("max")
                    bd = grade.get("breakdown")
                    if (
                        not _is_nonneg_int(total)
                        or not _is_nonneg_int(max_pts)
                        or not isinstance(bd, dict)
                        or not all(_is_nonneg_int(v) for v in bd.values())
                    ):
                        errors.append(f"{gid}.predictions[{model_id}].grade: invalid integer totals or breakdown")
                    elif total != sum(bd.values()) or total > max_pts:
                        errors.append(
                            f"{gid}.predictions[{model_id}].grade: total {total} inconsistent with breakdown sum {sum(bd.values())} and max {max_pts}"
                        )
                    else:
                        for cat, val in bd.items():
                            cap = RUBRIC_CAPS.get(cat)
                            if cap is not None and val > cap:
                                errors.append(
                                    f"{gid}.predictions[{model_id}].grade.breakdown.{cat}: {val} exceeds cap {cap}"
                                )

    return errors


def validate_all(data_dir: Path = DOCS_DATA_DIR) -> list[str]:
    errors: list[str] = []
    resolved_data_dir = data_dir.resolve()
    index_path = resolved_data_dir / "slates.json"
    if not index_path.is_file():
        return [f"Missing slates index: {index_path}"]

    index = json.loads(index_path.read_text(encoding="utf-8"))
    slates = index.get("slates")
    if not isinstance(slates, list) or not slates:
        return ["slates.json must contain a non-empty 'slates' list"]

    for entry in slates:
        rel_file = entry.get("file")
        if not isinstance(rel_file, str) or not rel_file:
            errors.append(f"Slate entry missing 'file': {entry}")
            continue

        slate_path = (resolved_data_dir.parent / rel_file).resolve()
        if resolved_data_dir not in slate_path.parents or slate_path.suffix != ".json":
            errors.append(f"Disallowed slate path outside data dir: {rel_file!r}")
            continue
        if not slate_path.is_file():
            errors.append(f"Missing slate file: {slate_path}")
            continue
        try:
            slate = json.loads(slate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON in {slate_path}: {exc}")
            continue

        errors.extend(validate_slate_dict(slate, slate_path.name))

    return errors


def build_multi_model_prompt(slate_id: str, data_dir: Path = DOCS_DATA_DIR) -> str:
    slate_path = _resolve_slate_path(slate_id, data_dir)
    if not slate_path.is_file():
        raise FileNotFoundError(f"Slate JSON not found: {slate_path}")
    slate = json.loads(slate_path.read_text(encoding="utf-8"))
    lines = [
        f"Use your `nfl-game-scout` skill to run a full, live-verified pregame scouting dossier for EVERY game in **{slate['title']}** ({slate['week']}).",
        "",
        "Matchups:",
    ]
    for idx, g in enumerate(slate["games"], start=1):
        lines.append(
            f"{idx}. `{g['id']}` — **{g['away']['name']} ({g['away']['record']}) at {g['home']['name']} ({g['home']['record']})** "
            f"| {g['label']} | {g['venue']['stadium']} ({g['venue']['city']})"
        )
    lines.extend(
        [
            "",
            "For each game, verify who started last week at QB and current official depth charts/inactives, intra-division record (if divisional), betting lines, weather/altitude physics, kicker career longs, roster ages, assigned referee crew tendencies, last-30-day social media & locker-room sentiment (-5 to +5), full 4-tier GM-to-assistant-position-coach org chart, and one grounded Crazy Surprise Call.",
            "",
            "Then return a JSON array (one object per game) matching this exact schema so I can run `python3 scripts/arena_cli.py ingest`:",
            "```json",
            json.dumps(
                [
                    {
                        "gameId": slate["games"][0]["id"],
                        "modelId": "claude-sonnet-5-5",
                        "modelLabel": "Claude Sonnet 5.5",
                        "modelMeta": {
                            "provider": "anthropic",
                            "exactModelId": "claude-sonnet-5-5",
                            "harness": "claude-desktop",
                            "webSearchEnabled": True,
                        },
                        "winnerAbbr": "TEAM",
                        "projectedScore": {"away": 24, "home": 27},
                        "spreadLean": "TEAM -2.5",
                        "totalLean": "Over 50.5",
                        "primaryKey": "One-sentence decisive tactical factor",
                        "refCall": "Assigned Head Ref — expected impact",
                        "sentimentEdge": "AWAY +X vs HOME +Y — contrarian note",
                        "hiddenMismatch": "Specific position/assistant coach duel",
                        "surpriseCall": {
                            "title": "Headline Crazy Surprise Call",
                            "detail": "Exact rule/play mechanic and why tonight's physics/refs/coaches make it live",
                            "ingredients": ["Ingredient 1", "Ingredient 2", "Ingredient 3"],
                            "probability": "~8%",
                        },
                        "bonusSleeper": {
                            "title": "Secondary sleeper",
                            "detail": "1-sentence sleeper projection",
                            "probability": "~6%",
                        },
                    }
                ],
                indent=2,
            ),
            "```",
        ]
    )
    return "\n".join(lines)


def ingest_predictions(slate_id: str, input_file: Path, game_id: str | None, data_dir: Path = DOCS_DATA_DIR) -> int:
    slate_path = _resolve_slate_path(slate_id, data_dir)
    if not slate_path.is_file():
        raise FileNotFoundError(f"Slate file not found: {slate_path}")

    slate = json.loads(slate_path.read_text(encoding="utf-8"))
    payload = json.loads(input_file.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    games_by_id = {g["id"]: g for g in slate["games"]}

    for item in items:
        target_gid = item.get("gameId") or game_id
        if not target_gid or target_gid not in games_by_id:
            raise ValueError(f"Unknown or missing gameId: {target_gid!r}")
        model_id = item.get("modelId", "")
        if not isinstance(model_id, str) or not SAFE_ID_RE.fullmatch(model_id):
            raise ValueError(f"Invalid modelId {model_id!r}; expected ^[a-z0-9-]+$")

        receipt_url = item.get("receiptUrl") or (
            f"https://github.com/TheScottyB/nfl-game-scout/commits/main/docs/data/{slate_id}.json"
        )
        if not _is_https_url(receipt_url):
            raise ValueError(f"Invalid receiptUrl {receipt_url!r}; must start with https://")

        proj = item.get("projectedScore")
        if not isinstance(proj, dict) or not _is_nonneg_int(proj.get("away")) or not _is_nonneg_int(proj.get("home")):
            raise ValueError(f"Invalid projectedScore {proj!r}; expected non-negative integer 'away' and 'home'")

        game = games_by_id[target_gid]
        entry = {
            "modelId": model_id,
            "modelLabel": item.get("modelLabel", model_id.title()),
            "status": item.get("status", "locked"),
            "publishedAtUtc": item.get("publishedAtUtc") or now_utc,
            "receiptUrl": receipt_url,
            "winnerAbbr": item["winnerAbbr"],
            "projectedScore": proj,
            "spreadLean": item["spreadLean"],
            "totalLean": item["totalLean"],
            "primaryKey": item["primaryKey"],
            "refCall": item["refCall"],
            "sentimentEdge": item["sentimentEdge"],
            "hiddenMismatch": item["hiddenMismatch"],
            "surpriseCall": item["surpriseCall"],
            "bonusSleeper": item.get("bonusSleeper"),
            "grade": item.get("grade"),
        }
        if item.get("modelMeta") is not None:
            entry["modelMeta"] = item["modelMeta"]
        preds = game["predictions"]
        replaced = False
        for idx, existing in enumerate(preds):
            if existing.get("modelId") == model_id:
                preds[idx] = entry
                replaced = True
                break
        if not replaced:
            preds.append(entry)
        updated += 1

    slate["updatedAtUtc"] = now_utc

    errors = validate_slate_dict(slate, slate_path.name)
    if errors:
        raise ValueError("Validation failed prior to write:\n" + "\n".join(errors))

    _atomic_write_json(slate_path, slate)
    return updated


def _parse_breakdown(raw: str) -> dict[str, int]:
    expected = ("winner", "scoreMargin", "spreadTotal", "winScriptRef", "orgSentiment", "surpriseBonus")
    parsed: dict[str, int] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise ValueError(f"Invalid breakdown item {pair!r}; expected key=int")
        k, v = pair.split("=", 1)
        k = k.strip()
        if k not in expected:
            raise ValueError(f"Unexpected breakdown key {k!r}; expected one of {expected}")
        parsed[k] = int(v.strip())
    if set(parsed.keys()) != set(expected):
        raise ValueError(f"Breakdown must include all 6 keys {expected}; got {tuple(parsed.keys())}")
    return parsed


def grade_prediction(
    slate_id: str,
    game_id: str,
    away_score: int,
    home_score: int,
    summary: str,
    freak_outcome: str,
    model_id: str,
    breakdown: dict[str, int],
    notes: str,
    data_dir: Path = DOCS_DATA_DIR,
) -> int:
    slate_path = _resolve_slate_path(slate_id, data_dir)
    if not slate_path.is_file():
        raise FileNotFoundError(f"Slate file not found: {slate_path}")
    if not SAFE_ID_RE.fullmatch(model_id):
        raise ValueError(f"Invalid modelId: {model_id!r}")
    if not (_is_nonneg_int(away_score) and _is_nonneg_int(home_score)):
        raise ValueError("Scores must be non-negative integers")

    slate = json.loads(slate_path.read_text(encoding="utf-8"))
    games_by_id = {g["id"]: g for g in slate["games"]}
    if game_id not in games_by_id:
        raise ValueError(f"Unknown gameId: {game_id!r}")

    game = games_by_id[game_id]
    if away_score > home_score:
        winner = game["away"]["abbr"]
    elif home_score > away_score:
        winner = game["home"]["abbr"]
    else:
        winner = "TIE"

    game["result"] = {
        "finalScore": {"away": away_score, "home": home_score},
        "winnerAbbr": winner,
        "summary": summary,
        "freakPlayOutcome": freak_outcome,
    }

    target_pred = next((p for p in game["predictions"] if p.get("modelId") == model_id), None)
    if not target_pred:
        raise ValueError(f"Prediction for model {model_id!r} not found in game {game_id!r}")

    total = sum(breakdown.values())
    target_pred["status"] = "graded"
    target_pred["grade"] = {
        "total": total,
        "max": 100,
        "breakdown": breakdown,
        "notes": notes,
    }
    slate["updatedAtUtc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    errors = validate_slate_dict(slate, slate_path.name)
    if errors:
        raise ValueError("Validation failed prior to write:\n" + "\n".join(errors))

    _atomic_write_json(slate_path, slate)
    return total


def _slugify_model_id(exact_model_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", exact_model_id.lower()).strip("-")
    if not slug or not SAFE_ID_RE.fullmatch(slug):
        raise ValueError(f"Could not build valid modelId slug from {exact_model_id!r}")
    return slug


def _format_model_label(provider: str, exact_model_id: str) -> str:
    tokens = re.split(r"[-_]+", exact_model_id.strip())
    out: list[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok.isdigit() and idx + 1 < len(tokens) and tokens[idx + 1].isdigit():
            # Keep dotted version pairs like 3.1 / 3.6 together when slugged from 3-1
            if out and out[-1].lower() == "gemini":
                out.append(f"{tok}.{tokens[idx + 1]}")
                idx += 2
                continue
            if out and out[-1].lower() in {"sonnet", "opus", "haiku", "claude", "gpt"}:
                out.append(f"{tok}.{tokens[idx + 1]}")
                idx += 2
                continue
        out.append(tok.upper() if tok.lower() in {"gpt", "o1", "o3", "o4"} else tok.capitalize())
        idx += 1
    label = " ".join(out)
    # Restore dotted version numbers such as Gemini 3.1 Pro Preview
    label = re.sub(r"\b(\d)\.(\d)\b", r"\1.\2", label)
    return label or f"{provider.title()} ({exact_model_id})"


def _parse_model_spec(raw_spec: str) -> tuple[str, str]:
    if ":" not in raw_spec:
        raise ValueError(
            f"Invalid model spec {raw_spec!r}; expected '<provider>:<model>' (providers: {sorted(SUPPORTED_PROVIDERS)})"
        )
    provider, model_name = raw_spec.split(":", 1)
    provider = provider.strip().lower()
    model_name = model_name.strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported provider {provider!r}; expected one of {sorted(SUPPORTED_PROVIDERS)}")
    if not model_name:
        raise ValueError(f"Empty model name in spec {raw_spec!r}")
    return provider, model_name


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Model output did not contain a JSON object: {raw_text[:300]!r}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from model, got {type(parsed).__name__}")
    return parsed


def build_game_prediction_prompt(slate: dict[str, Any], game: dict[str, Any]) -> str:
    snapshot = {
        "slateId": slate.get("slateId"),
        "week": slate.get("week"),
        "gameId": game["id"],
        "label": game["label"],
        "kickoffUtc": game["kickoffUtc"],
        "broadcast": game["broadcast"],
        "away": game["away"],
        "home": game["home"],
        "venue": game["venue"],
        "odds": game["odds"],
        "weather": game["weather"],
        "injuries": game["injuries"],
        "referee": game["referee"],
        "divisionContext": game.get("divisionContext"),
        "winScripts": game["winScripts"],
        "sentiment30d": game["sentiment30d"],
        "orgChart": game["orgChart"],
    }
    schema_hint = {
        "winnerAbbr": f"{game['away']['abbr']} or {game['home']['abbr']}",
        "projectedScore": {"away": 20, "home": 17},
        "spreadLean": "TEAM -4.5",
        "totalLean": "Under 37.5",
        "primaryKey": "One-sentence decisive tactical factor grounded in the verified snapshot",
        "refCall": "Assigned Head Ref — specific expected impact on tonight's matchup",
        "sentimentEdge": "AWAY +X vs HOME +Y — contrarian or locker-room takeaway",
        "hiddenMismatch": "Specific Tier 3/4 position or assistant coach duel",
        "surpriseCall": {
            "title": "Headline Crazy Surprise Call",
            "detail": "Exact play/rule mechanic grounded in 3+ verified ingredients",
            "ingredients": ["Ingredient 1", "Ingredient 2", "Ingredient 3"],
            "probability": "~9%",
        },
        "bonusSleeper": {
            "title": "Secondary sleeper",
            "detail": "1-sentence sleeper projection",
            "probability": "~7%",
        },
    }
    return (
        "You are an independent NFL Game Scout analyst competing in the 100-point Multi-Model Arena.\n"
        "All current-season rosters, starting QBs, depth-chart updates, injuries, weather, odds, referee tendencies, "
        "30-day sentiment, and 4-tier coaching org charts are already verified in the JSON snapshot below. "
        "Do NOT call any tools or read any files.\n\n"
        "Analyze the verified matchup independently and make your own sharpest football prediction (score, winner, "
        "spread/total lean, key mismatch, referee impact, and grounded Crazy Surprise Call using 3+ real ingredients).\n\n"
        "VERIFIED MATCHUP SNAPSHOT:\n"
        f"{json.dumps(snapshot, indent=2)}\n\n"
        "Return STRICTLY one raw JSON object (no markdown prose outside JSON) matching this exact structure:\n"
        f"{json.dumps(schema_hint, indent=2)}\n"
    )


def _call_gemini_generate_content(model: str, prompt: str, timeout_sec: int) -> tuple[dict[str, Any], str, dict[str, Any]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is required for gemini:<model>")

    quoted = urllib.parse.quote(model, safe="")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{quoted}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.35,
            "responseMimeType": "application/json",
        },
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    backoffs = (2, 5, 10)
    body: dict[str, Any] | None = None
    last_err: Exception | None = None
    for attempt in range(len(backoffs) + 1):
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as exc:
            err_text = exc.read().decode("utf-8", errors="replace")[:500]
            last_err = RuntimeError(f"Gemini API HTTP {exc.code} for {model}: {err_text}")
            if exc.code == 429 and ("limit: 0" in err_text or "requests per day" in err_text):
                raise last_err from None
            if exc.code in (429, 500, 502, 503, 504) and attempt < len(backoffs):
                time.sleep(backoffs[attempt])
                continue
            raise last_err from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = RuntimeError(f"Gemini API network error for {model}: {exc}")
            if attempt < len(backoffs):
                time.sleep(backoffs[attempt])
                continue
            raise last_err from None
    if body is None:
        assert last_err is not None
        raise last_err

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini API returned no candidates for {model}: {body}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text_chunks = [p["text"] for p in parts if isinstance(p, dict) and not p.get("thought") and "text" in p]
    if not text_chunks:
        text_chunks = [p["text"] for p in parts if isinstance(p, dict) and "text" in p]
    if not text_chunks:
        raise RuntimeError(f"Gemini API returned empty text parts for {model}")

    parsed = _extract_json_object("\n".join(text_chunks))
    exact_model = str(body.get("modelVersion") or model)
    um = body.get("usageMetadata") or {}
    usage = {
        k: v
        for k, v in (
            ("inputTokens", um.get("promptTokenCount")),
            ("outputTokens", um.get("candidatesTokenCount")),
            ("totalTokens", um.get("totalTokenCount")),
        )
        if isinstance(v, int)
    }
    return parsed, exact_model, usage


def _call_claude_cli(model: str, prompt: str, timeout_sec: int) -> tuple[dict[str, Any], str, dict[str, Any]]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("`claude` CLI binary not found on PATH for claude-cli:<model>")

    res = subprocess.run(
        [claude_bin, "-p", "--output-format", "json", "--model", model, "--max-turns", "1"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"claude-cli failed ({res.returncode}): {res.stderr[:400] or res.stdout[:400]}")

    env = json.loads(res.stdout)
    if env.get("is_error"):
        raise RuntimeError(f"claude-cli returned error: {env.get('result')}")

    raw_result = env.get("result")
    if isinstance(raw_result, dict):
        parsed = raw_result
    elif isinstance(raw_result, str) and raw_result.strip():
        parsed = _extract_json_object(raw_result)
    elif isinstance(env.get("structured_output"), dict):
        parsed = env["structured_output"]
    else:
        raise RuntimeError(f"claude-cli envelope missing 'result': {res.stdout[:300]}")

    model_usage = env.get("modelUsage") or {}
    exact_model = model
    usage: dict[str, Any] = {}
    if isinstance(model_usage, dict) and model_usage:
        first_key = next(iter(model_usage.keys()))
        mu = model_usage[first_key] or {}
        exact_model = str(mu.get("canonicalModel") or first_key or model)
        for uk, mk in (
            ("inputTokens", "inputTokens"),
            ("outputTokens", "outputTokens"),
            ("cacheReadInputTokens", "cacheReadInputTokens"),
        ):
            if isinstance(mu.get(mk), int):
                usage[uk] = mu[mk]
    if isinstance(env.get("total_cost_usd"), (int, float)):
        usage["costUsd"] = round(float(env["total_cost_usd"]), 6)
    return parsed, exact_model, usage


def _call_anthropic_api(model: str, prompt: str, timeout_sec: int) -> tuple[dict[str, Any], str, dict[str, Any]]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is required for anthropic:<model>")

    payload = {
        "model": model,
        "max_tokens": 2048,
        "temperature": 0.35,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Anthropic API HTTP {exc.code} for {model}: {err_text}") from None

    chunks = [b["text"] for b in body.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
    if not chunks:
        raise RuntimeError(f"Anthropic API returned no text content for {model}")
    parsed = _extract_json_object("\n".join(chunks))
    exact_model = str(body.get("model") or model)
    raw_u = body.get("usage") or {}
    usage = {
        k: v
        for k, v in (
            ("inputTokens", raw_u.get("input_tokens")),
            ("outputTokens", raw_u.get("output_tokens")),
        )
        if isinstance(v, int)
    }
    return parsed, exact_model, usage


def _call_openai_api(model: str, prompt: str, timeout_sec: int) -> tuple[dict[str, Any], str, dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is required for openai:<model>")

    payload = {
        "model": model,
        "temperature": 0.35,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenAI API HTTP {exc.code} for {model}: {err_text}") from None

    msg = body["choices"][0]["message"]["content"]
    parsed = _extract_json_object(msg)
    exact_model = str(body.get("model") or model)
    raw_u = body.get("usage") or {}
    usage = {
        k: v
        for k, v in (
            ("inputTokens", raw_u.get("prompt_tokens")),
            ("outputTokens", raw_u.get("completion_tokens")),
            ("totalTokens", raw_u.get("total_tokens")),
        )
        if isinstance(v, int)
    }
    return parsed, exact_model, usage


def _invoke_provider(provider: str, model: str, prompt: str, timeout_sec: int) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    if provider == "gemini":
        parsed, exact, usage = _call_gemini_generate_content(model, prompt, timeout_sec)
        return parsed, exact, usage, "arena-cli-gemini-api"
    if provider == "claude-cli":
        parsed, exact, usage = _call_claude_cli(model, prompt, timeout_sec)
        return parsed, exact, usage, "arena-cli-claude-cli"
    if provider == "anthropic":
        parsed, exact, usage = _call_anthropic_api(model, prompt, timeout_sec)
        return parsed, exact, usage, "arena-cli-anthropic-api"
    if provider == "openai":
        parsed, exact, usage = _call_openai_api(model, prompt, timeout_sec)
        return parsed, exact, usage, "arena-cli-openai-api"
    raise ValueError(f"Unsupported provider: {provider!r}")


def run_models_for_slate(
    slate_id: str,
    model_specs: list[str],
    game_id: str | None = None,
    timeout_sec: int = 180,
    dry_run: bool = False,
    data_dir: Path = DOCS_DATA_DIR,
) -> list[dict[str, Any]]:
    slate_path = _resolve_slate_path(slate_id, data_dir)
    if not slate_path.is_file():
        raise FileNotFoundError(f"Slate file not found: {slate_path}")
    if not model_specs:
        raise ValueError("At least one --models spec (<provider>:<model>) is required")

    parsed_specs = [_parse_model_spec(s) for s in model_specs]
    slate = json.loads(slate_path.read_text(encoding="utf-8"))
    target_games = slate.get("games", [])
    if game_id:
        target_games = [g for g in target_games if g.get("id") == game_id]
        if not target_games:
            raise ValueError(f"Unknown gameId {game_id!r} in slate {slate_id!r}")

    summaries: list[dict[str, Any]] = []
    if dry_run:
        for game in target_games:
            prompt = build_game_prediction_prompt(slate, game)
            sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            for provider, model_name in parsed_specs:
                summaries.append(
                    {
                        "gameId": game["id"],
                        "provider": provider,
                        "requestedModel": model_name,
                        "modelId": _slugify_model_id(model_name),
                        "promptSha256": sha,
                        "dryRun": True,
                    }
                )
        return summaries

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for game in target_games:
        prompt = build_game_prediction_prompt(slate, game)
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

        def _run_one(spec: tuple[str, str]) -> dict[str, Any]:
            provider, req_model = spec
            t0 = time.monotonic()
            pred_obj, exact_model, usage, harness = _invoke_provider(provider, req_model, prompt, timeout_sec)
            latency = round(time.monotonic() - t0, 2)
            run_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            model_id = _slugify_model_id(exact_model)
            model_label = _format_model_label(provider, exact_model)
            proj = pred_obj.get("projectedScore")
            if not isinstance(proj, dict) or not _is_nonneg_int(proj.get("away")) or not _is_nonneg_int(proj.get("home")):
                raise ValueError(f"{exact_model} returned invalid projectedScore: {proj!r}")
            sc = pred_obj.get("surpriseCall")
            if not isinstance(sc, dict) or not sc.get("title") or not sc.get("detail"):
                raise ValueError(f"{exact_model} returned invalid surpriseCall: {sc!r}")

            return {
                "modelId": model_id,
                "modelLabel": model_label,
                "modelMeta": {
                    "provider": "anthropic" if provider == "claude-cli" else provider,
                    "requestedModel": req_model,
                    "exactModelId": exact_model,
                    "harness": harness,
                    "webSearchEnabled": False,
                    "promptSha256": prompt_sha,
                    "runAtUtc": run_utc,
                    "latencySec": latency,
                    "usage": usage,
                },
                "status": "locked",
                "publishedAtUtc": run_utc,
                "receiptUrl": f"https://github.com/TheScottyB/nfl-game-scout/commits/main/docs/data/{slate_id}.json",
                "winnerAbbr": str(pred_obj["winnerAbbr"]),
                "projectedScore": {"away": int(proj["away"]), "home": int(proj["home"])},
                "spreadLean": str(pred_obj["spreadLean"]),
                "totalLean": str(pred_obj["totalLean"]),
                "primaryKey": str(pred_obj["primaryKey"]),
                "refCall": str(pred_obj["refCall"]),
                "sentimentEdge": str(pred_obj["sentimentEdge"]),
                "hiddenMismatch": str(pred_obj["hiddenMismatch"]),
                "surpriseCall": sc,
                "bonusSleeper": pred_obj.get("bonusSleeper"),
                "grade": None,
            }

        results_by_order: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(4, len(parsed_specs))) as pool:
            futs = [pool.submit(_run_one, spec) for spec in parsed_specs]
            for fut in futs:
                results_by_order.append(fut.result())

        preds = game.setdefault("predictions", [])
        for entry in results_by_order:
            replaced = False
            for idx, existing in enumerate(preds):
                if existing.get("modelId") == entry["modelId"]:
                    preds[idx] = entry
                    replaced = True
                    break
            if not replaced:
                preds.append(entry)
            summaries.append(
                {
                    "gameId": game["id"],
                    "modelId": entry["modelId"],
                    "modelLabel": entry["modelLabel"],
                    "exactModelId": entry["modelMeta"]["exactModelId"],
                    "winnerAbbr": entry["winnerAbbr"],
                    "projectedScore": entry["projectedScore"],
                    "latencySec": entry["modelMeta"]["latencySec"],
                    "promptSha256": prompt_sha,
                }
            )

    slate["updatedAtUtc"] = now_utc
    errors = validate_slate_dict(slate, slate_path.name)
    if errors:
        raise ValueError("Validation failed prior to write:\n" + "\n".join(errors))
    _atomic_write_json(slate_path, slate)
    return summaries


def main() -> int:
    parser = argparse.ArgumentParser(description="NFL Game Scout Arena CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="Validate all slate JSON files under docs/data")

    p_prompt = sub.add_parser("prompt", help="Print ready-to-paste multi-model prompt for a slate")
    p_prompt.add_argument("--slate", default="2026-black-friday")

    p_run = sub.add_parser("run", help="Run multiple AI models against a slate and record exact model provenance")
    p_run.add_argument("--slate", required=True)
    p_run.add_argument(
        "--models",
        required=True,
        help="Comma-separated <provider>:<model> specs (e.g. gemini:gemini-3.1-pro-preview,gemini:gemini-3.6-flash,claude-cli:sonnet)",
    )
    p_run.add_argument("--game", default=None, help="Optional single gameId to run")
    p_run.add_argument("--timeout", type=int, default=180, help="Per-model request timeout in seconds")
    p_run.add_argument("--dry-run", action="store_true", help="Print resolved specs and promptSha256 without calling APIs")

    p_ingest = sub.add_parser("ingest", help="Ingest a model prediction JSON object or list into a slate")
    p_ingest.add_argument("--slate", required=True)
    p_ingest.add_argument("--file", required=True, type=Path)
    p_ingest.add_argument("--game", default=None, help="Optional gameId if ingesting a single object without gameId")

    p_grade = sub.add_parser("grade", help="Record final score and 100-point rubric grade for a model prediction")
    p_grade.add_argument("--slate", required=True)
    p_grade.add_argument("--game", required=True)
    p_grade.add_argument("--away-score", required=True, type=int)
    p_grade.add_argument("--home-score", required=True, type=int)
    p_grade.add_argument("--summary", required=True)
    p_grade.add_argument("--freak-outcome", required=True)
    p_grade.add_argument("--model", required=True)
    p_grade.add_argument(
        "--breakdown",
        required=True,
        help="Comma-separated rubric scores: winner=N,scoreMargin=N,spreadTotal=N,winScriptRef=N,orgSentiment=N,surpriseBonus=N",
    )
    p_grade.add_argument("--notes", required=True)

    args = parser.parse_args()
    if args.cmd == "validate":
        errors = validate_all()
        if errors:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1
        print("OK: All NFL Game Scout slates validated cleanly.")
        return 0

    if args.cmd == "prompt":
        print(build_multi_model_prompt(args.slate))
        return 0

    if args.cmd == "run":
        specs = [s.strip() for s in args.models.split(",") if s.strip()]
        summaries = run_models_for_slate(
            slate_id=args.slate,
            model_specs=specs,
            game_id=args.game,
            timeout_sec=args.timeout,
            dry_run=args.dry_run,
        )
        print(json.dumps(summaries, indent=2))
        return 0

    if args.cmd == "ingest":
        count = ingest_predictions(args.slate, args.file, args.game)
        print(f"OK: Ingested {count} prediction(s) into {args.slate} and validated cleanly.")
        return 0

    if args.cmd == "grade":
        bd = _parse_breakdown(args.breakdown)
        total = grade_prediction(
            slate_id=args.slate,
            game_id=args.game,
            away_score=args.away_score,
            home_score=args.home_score,
            summary=args.summary,
            freak_outcome=args.freak_outcome,
            model_id=args.model,
            breakdown=bd,
            notes=args.notes,
        )
        print(f"OK: Graded {args.model} on {args.game}: {total}/100.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
