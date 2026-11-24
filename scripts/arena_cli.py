#!/usr/bin/env python3
"""CLI helper to validate NFL Game Scout slates, generate multi-model prompts, and ingest model predictions."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA_DIR = REPO_ROOT / "docs" / "data"
SLATES_INDEX = DOCS_DATA_DIR / "slates.json"

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


def _parse_iso8601(value: str, context: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}: expected non-empty ISO-8601 timestamp, got {value!r}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{context}: invalid ISO-8601 timestamp {value!r}") from exc


def validate_all(data_dir: Path = DOCS_DATA_DIR) -> list[str]:
    errors: list[str] = []
    index_path = data_dir / "slates.json"
    if not index_path.is_file():
        return [f"Missing slates index: {index_path}"]

    index = json.loads(index_path.read_text(encoding="utf-8"))
    slates = index.get("slates")
    if not isinstance(slates, list) or not slates:
        return ["slates.json must contain a non-empty 'slates' list"]

    for entry in slates:
        rel_file = entry.get("file")
        if not rel_file:
            errors.append(f"Slate entry missing 'file': {entry}")
            continue
        slate_path = data_dir.parent / rel_file
        if not slate_path.is_file():
            errors.append(f"Missing slate file: {slate_path}")
            continue
        try:
            slate = json.loads(slate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON in {slate_path}: {exc}")
            continue

        try:
            _parse_iso8601(slate.get("updatedAtUtc", ""), f"{slate_path.name}.updatedAtUtc")
        except ValueError as exc:
            errors.append(str(exc))

        games = slate.get("games")
        if not isinstance(games, list) or not games:
            errors.append(f"{slate_path.name}: 'games' must be a non-empty list")
            continue

        for game in games:
            gid = game.get("id", "<unknown>")
            missing = REQUIRED_GAME_KEYS - set(game.keys())
            if missing:
                errors.append(f"{slate_path.name}:{gid}: missing game keys {sorted(missing)}")
                continue

            try:
                _parse_iso8601(game["kickoffUtc"], f"{gid}.kickoffUtc")
            except ValueError as exc:
                errors.append(str(exc))

            sent = game["sentiment30d"]
            for key in ("awayScore", "homeScore"):
                score = sent.get(key)
                if not isinstance(score, (int, float)) or not (-5 <= score <= 5):
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
                if status not in {"locked", "pending", "graded"}:
                    errors.append(f"{gid}.predictions[{model_id}]: invalid status {status!r}")
                if status in {"locked", "graded"}:
                    try:
                        _parse_iso8601(pred.get("publishedAtUtc", ""), f"{gid}.{model_id}.publishedAtUtc")
                    except ValueError as exc:
                        errors.append(str(exc))
                    sc = pred.get("surpriseCall") or {}
                    if not sc.get("title") or not sc.get("detail"):
                        errors.append(f"{gid}.predictions[{model_id}]: locked/graded entry missing surpriseCall title/detail")
                    if not pred.get("projectedScore") or not pred.get("winnerAbbr"):
                        errors.append(f"{gid}.predictions[{model_id}]: locked/graded entry missing winnerAbbr or projectedScore")

    return errors


def build_multi_model_prompt(slate_id: str, data_dir: Path = DOCS_DATA_DIR) -> str:
    slate_path = data_dir / f"{slate_id}.json"
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
    slate_path = data_dir / f"{slate_id}.json"
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
        game = games_by_id[target_gid]
        model_id = item["modelId"]
        entry = {
            "modelId": model_id,
            "modelLabel": item.get("modelLabel", model_id.title()),
            "status": item.get("status", "locked"),
            "publishedAtUtc": item.get("publishedAtUtc") or now_utc,
            "receiptUrl": item.get("receiptUrl") or f"https://github.com/TheScottyB/nfl-game-scout/commits/main/docs/data/{slate_id}.json",
            "winnerAbbr": item["winnerAbbr"],
            "projectedScore": item["projectedScore"],
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
    slate_path.write_text(json.dumps(slate, indent=2) + "\n", encoding="utf-8")

    errors = validate_all(data_dir)
    if errors:
        raise ValueError("Validation failed after ingest:\n" + "\n".join(errors))
    return updated


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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
