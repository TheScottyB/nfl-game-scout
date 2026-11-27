#!/usr/bin/env python3
"""Upload an NFL Game Scout YouTube Short via YouTube Data API v3 OAuth 2.0 resumable upload."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

YOUTUBE_API_HELPER = (
    Path.home()
    / ".gemini"
    / "config"
    / "plugins"
    / "video-watcher"
    / "skills"
    / "video-watcher"
    / "scripts"
    / "youtube_api.py"
)
UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
MAX_TITLE_LEN = 100


def _load_access_token() -> str:
    if not YOUTUBE_API_HELPER.is_file():
        raise FileNotFoundError(f"YouTube OAuth helper not found: {YOUTUBE_API_HELPER}")
    spec = importlib.util.spec_from_file_location("video_watcher_youtube_api", YOUTUBE_API_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec from {YOUTUBE_API_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    token = module.get_valid_access_token()
    if not token:
        raise RuntimeError("No valid YouTube OAuth access token available; run `youtube_api.py auth login`.")
    return str(token)


def upload_short(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str = "public",
    category_id: str = "17",
) -> dict[str, str]:
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    clean_title = " ".join(title.split())
    if not clean_title or len(clean_title) > MAX_TITLE_LEN:
        raise ValueError(f"Title must be 1..{MAX_TITLE_LEN} chars, got {len(clean_title)}")
    if privacy not in {"public", "unlisted", "private"}:
        raise ValueError(f"Invalid privacy status: {privacy!r}")

    token = _load_access_token()
    video_bytes = video_path.read_bytes()
    content_length = len(video_bytes)

    metadata = {
        "snippet": {
            "title": clean_title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    init_req = urllib.request.Request(
        UPLOAD_INIT_URL,
        data=json.dumps(metadata).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(content_length),
            "X-Upload-Content-Type": "video/mp4",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(init_req, timeout=60) as init_resp:
            upload_url = init_resp.headers.get("Location")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Resumable upload init failed (HTTP {exc.code}): {err_body}") from None

    if not upload_url:
        raise RuntimeError("YouTube resumable upload init response missing Location header")

    put_req = urllib.request.Request(
        upload_url,
        data=video_bytes,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "video/mp4",
            "Content-Length": str(content_length),
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(put_req, timeout=300) as put_resp:
            resp_data = json.loads(put_resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Resumable media upload failed (HTTP {exc.code}): {err_body}") from None

    video_id = str(resp_data.get("id") or "")
    if not video_id:
        raise RuntimeError(f"YouTube upload response missing video id: {resp_data}")
    return {
        "videoId": video_id,
        "shortsUrl": f"https://youtube.com/shorts/{video_id}",
        "watchUrl": f"https://www.youtube.com/watch?v={video_id}",
        "title": clean_title,
        "privacyStatus": str(resp_data.get("status", {}).get("privacyStatus") or privacy),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload an NFL Game Scout Short to YouTube via Data API v3")
    parser.add_argument("--file", required=True, type=Path, help="Path to MP4 file")
    parser.add_argument("--title", required=True, help="Video title (<= 100 chars)")
    parser.add_argument("--description", default=None, help="Video description text")
    parser.add_argument("--description-file", type=Path, default=None, help="Path to text file with description")
    parser.add_argument("--privacy", choices=["public", "unlisted", "private"], default="public")
    parser.add_argument("--tags", default="NFL,NFLPredictions,AI,SportsAnalytics,Shorts", help="Comma-separated tags")
    parser.add_argument("--category-id", default="17", help="YouTube category ID (default: 17 Sports)")
    args = parser.parse_args()

    if args.description_file is not None:
        description = args.description_file.read_text(encoding="utf-8")
    elif args.description is not None:
        description = args.description
    else:
        raise ValueError("Provide either --description or --description-file")

    tags = [t.strip().lstrip("#") for t in args.tags.split(",") if t.strip()]
    result = upload_short(
        video_path=args.file,
        title=args.title,
        description=description,
        tags=tags,
        privacy=args.privacy,
        category_id=args.category_id,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
