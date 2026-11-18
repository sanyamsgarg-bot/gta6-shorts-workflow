#!/usr/bin/env python3
"""Auto-discover trending GTA Online / GTA 6 video, render 9:16 Short, and upload to YouTube."""
import glob, json, os, pathlib, subprocess, sys

query = os.environ.get("SEARCH_QUERY", "").strip() or "GTA Online GTA 6 gameplay stream highlights"
url = os.environ.get("STREAM_URL", "").strip()
hist = pathlib.Path("processed_videos.json")
seen = set(json.loads(hist.read_text()).get("processed_video_ids", [])) if hist.exists() else set()
raw = subprocess.check_output(["yt-dlp", "--dump-single-json", "--flat-playlist", f"ytsearch15:{query}"], text=True) if not url else "{}"
entries = [e for e in json.loads(raw).get("entries", []) if e.get("id") and e.get("id") not in seen and (e.get("duration") or 600) >= 180]
entries.sort(key=lambda e: float(e.get("view_count") or 0), reverse=True)
chosen = entries[0] if (not url and entries) else {}
vid = chosen.get("id", "")
url = url or f"https://www.youtube.com/watch?v={vid}"
seen.update([vid] if vid else [])
hist.write_text(json.dumps({"processed_video_ids": sorted(seen)}, indent=2))
print(f"[Auto-Discovery] Selected GTA video: {chosen.get('title', url)} ({url})")
subprocess.run([sys.executable, "gta6_shorts_pipeline.py", "--url", url, "--max-clips", "1", "--output-dir", "output_shorts"], check=True)
cid, csec, rtok = os.environ.get("YT_CLIENT_ID", "").strip(), os.environ.get("YT_CLIENT_SECRET", "").strip(), os.environ.get("YT_REFRESH_TOKEN", "").strip()
mp4s = sorted(glob.glob("output_shorts/*.mp4"))
manifest = json.loads(pathlib.Path("output_shorts/publishing_manifest.json").read_text()) if pathlib.Path("output_shorts/publishing_manifest.json").exists() else [{}]
meta = manifest[0] if manifest else {}
if not (cid and csec and rtok and mp4s): print("[YouTube Upload] Set YT_CLIENT_ID, YT_CLIENT_SECRET, and YT_REFRESH_TOKEN in GitHub Secrets to enable live channel upload."); sys.exit(0)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
creds = Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token", client_id=cid, client_secret=csec, scopes=["https://www.googleapis.com/auth/youtube.upload"])
yt = build("youtube", "v3", credentials=creds)
body = {"snippet": {"title": meta.get("title", "GTA Online & GTA 6 Viral Highlight #GTA6 #Shorts")[:95], "description": meta.get("description", "Transformative 9:16 GTA highlight with AI commentary. #GTA6 #GTAOnline #Shorts"), "tags": ["GTA6", "GTAOnline", "GTAVI", "Shorts", "Gaming"], "categoryId": "20"}, "status": {"privacyStatus": os.environ.get("PRIVACY_STATUS", "public") or "public", "selfDeclaredMadeForKids": False}}
resp = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(mp4s[0], mimetype="video/mp4", resumable=True)).execute()
print(f"[YouTube Upload] Uploaded Short: https://youtube.com/shorts/{resp.get('id')}")
