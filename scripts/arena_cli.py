#!/usr/bin/env python3
"""CLI helper to validate NFL Game Scout slates, generate multi-model prompts, ingest predictions, and grade games."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA_DIR = REPO_ROOT / "docs" / "data"

SAFE_ID_RE = re.compile(r"^[a-z0-9-]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

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
    parsed = urlparse(val)
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
            "For each game, verify current-season injuries, betting lines, weather/altitude physics, kicker career longs, roster ages, assigned referee crew tendencies, last-30-day social media & locker-room sentiment (-5 to +5), full 4-tier GM-to-assistant-position-coach org chart, and one grounded Crazy Surprise Call.",
            "",
            "Then return a JSON array (one object per game) matching this exact schema so I can run `python3 scripts/arena_cli.py ingest`:",
            "```json",
            json.dumps(
                [
                    {
                        "gameId": slate["games"][0]["id"],
                        "modelId": "claude",
                        "modelLabel": "Claude (Claude Desktop)",
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


def main() -> int:
    parser = argparse.ArgumentParser(description="NFL Game Scout Arena CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="Validate all slate JSON files under docs/data")

    p_prompt = sub.add_parser("prompt", help="Print ready-to-paste multi-model prompt for a slate")
    p_prompt.add_argument("--slate", default="2026-thanksgiving")

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
