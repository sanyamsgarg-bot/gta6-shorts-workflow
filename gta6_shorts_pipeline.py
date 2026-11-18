#!/usr/bin/env python3
"""GTA 6 Viral Faceless Shorts & Reels Automation Pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class WordCue:
    word: str
    start: float
    end: float


@dataclass
class ViralClipSpec:
    clip_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    viral_score: int
    action_category: str
    hook_narration: str
    commentary_script: str
    title: str
    description: str
    hashtags: List[str]
    crop_mode: str = "blur_fill_action"
    contains_isolated_cutscene: bool = False
    caption_cues: List[WordCue] = field(default_factory=list)


GTA6_VIRAL_KEYWORDS = {
    "heist": 18, "police": 14, "cops": 14, "wanted": 16, "six stars": 22,
    "five stars": 18, "lucia": 12, "jason": 12, "vice city": 12, "leonida": 12,
    "stunt": 16, "exploded": 15, "insane": 14, "no way": 15, "clutch": 16,
    "npc": 14, "alligator": 16, "gator": 16, "drift": 13, "helicopters": 14,
    "wasted": 18, "busted": 16,
}

CUTSCENE_SPOILER_MARKERS = [
    "ending cutscene", "final credits", "story ending spoiler", "full cutscene only",
]


def fetch_stream_metadata(stream_url: str, work_dir: Path) -> dict[str, Any]:
    meta_path = work_dir / "stream_meta.json"
    res = subprocess.run(
        ["yt-dlp", "--dump-single-json", "--no-playlist", stream_url],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(res.stdout)
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def download_audio_track(stream_url: str, work_dir: Path) -> Path:
    audio_out = work_dir / "stream_audio.wav"
    subprocess.run(
        [
            "yt-dlp", "-f", "ba/b", "--no-playlist", "-x",
            "--audio-format", "wav",
            "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000",
            "-o", str(audio_out), stream_url,
        ],
        check=True,
    )
    return audio_out


def download_video_section(stream_url: str, start_sec: float, end_sec: float, out_path: Path) -> Path:
    section_arg = f"*{max(0.0, start_sec - 1.0):.2f}-{end_sec + 1.0:.2f}"
    subprocess.run(
        [
            "yt-dlp", "--no-playlist",
            "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b",
            "--download-sections", section_arg,
            "--force-keyframes-at-cuts",
            "--merge-output-format", "mp4",
            "-o", str(out_path), stream_url,
        ],
        check=True,
    )
    return out_path


def transcribe_stream_audio(audio_path: Path, model_size: str = "base.en") -> List[dict[str, Any]]:
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments_iter, _ = model.transcribe(str(audio_path), beam_size=5, word_timestamps=True, vad_filter=True)
    segments: List[dict[str, Any]] = []
    for seg in segments_iter:
        words = [
            {"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)}
            for w in (seg.words or []) if w.word.strip()
        ]
        segments.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip(), "words": words})
    return segments


def snap_window_to_sentences(
    segments: List[dict[str, Any]], raw_start: float, min_dur: float = 30.0, max_dur: float = 45.0
) -> tuple[float, float, List[WordCue]]:
    all_words = [
        WordCue(word=w["word"], start=float(w["start"]), end=float(w["end"]))
        for seg in segments for w in seg.get("words", [])
    ]
    if not all_words:
        return round(raw_start, 2), round(raw_start + 36.0, 2), []
    start_candidates = [w.start for w in all_words if w.start >= raw_start - 2.0]
    snapped_start = start_candidates[0] if start_candidates else raw_start
    valid_end_words = [w for w in all_words if snapped_start + min_dur <= w.end <= snapped_start + max_dur]
    punct_end_words = [w for w in valid_end_words if w.word.endswith((".", "!", "?"))]
    if punct_end_words:
        snapped_end = punct_end_words[-1].end + 0.25
    elif valid_end_words:
        snapped_end = valid_end_words[-1].end + 0.25
    else:
        snapped_end = snapped_start + ((min_dur + max_dur) / 2.0)
    snapped_end = max(snapped_start + min_dur, min(snapped_start + max_dur, snapped_end))
    window_words = [
        WordCue(word=w.word, start=round(max(0.0, w.start - snapped_start), 2), end=round(min(snapped_end - snapped_start, w.end - snapped_start), 2))
        for w in all_words if snapped_start <= w.start <= snapped_end
    ]
    return round(snapped_start, 2), round(snapped_end, 2), window_words


def score_candidate_windows_heuristic(
    segments: List[dict[str, Any]], heatmap: Optional[List[dict[str, float]]],
    min_dur: float = 30.0, max_dur: float = 45.0, max_clips: int = 3
) -> List[ViralClipSpec]:
    if not segments:
        return []
    stream_end = max(float(s["end"]) for s in segments)
    candidates: List[ViralClipSpec] = []
    cursor, idx = 0.0, 1
    while cursor + min_dur <= stream_end:
        s_start, s_end, cues = snap_window_to_sentences(segments, cursor, min_dur, max_dur)
        dur = round(s_end - s_start, 2)
        window_text = " ".join(s["text"] for s in segments if float(s["end"]) > s_start and float(s["start"]) < s_end)
        lower_text = window_text.lower()
        if any(m in lower_text for m in CUTSCENE_SPOILER_MARKERS):
            cursor += 15.0
            continue
        action_score = min(40, sum(w for kw, w in GTA6_VIRAL_KEYWORDS.items() if kw in lower_text))
        exclamations = window_text.count("!") * 4 + window_text.count("?") * 2
        heatmap_boost = 0.0
        if heatmap:
            overlaps = [float(h.get("value", 0.0)) for h in heatmap if float(h.get("end_time", 0.0)) > s_start and float(h.get("start_time", 0.0)) < s_end]
            if overlaps:
                heatmap_boost = max(overlaps) * 20.0
        excitement_score = min(25, int(exclamations + heatmap_boost))
        wps = len(cues) / max(1.0, dur)
        pacing_score = 20 if 2.0 <= wps <= 4.5 else max(8, int(20 - abs(wps - 3.0) * 4))
        total_score = min(100, action_score + excitement_score + pacing_score + 15)
        hook_line = "One mistake in this Leonida police chase ends the entire run—watch how the physics react."
        commentary = f"{hook_line} Notice how the vehicle weight shifts around the corner right before the roadblock closes in, turning a routine Vice City escape into a last-second stunt."
        candidates.append(
            ViralClipSpec(
                clip_id=f"gta6_short_{idx:02d}", start_sec=s_start, end_sec=s_end, duration_sec=dur,
                viral_score=total_score, action_category="police_chase_stunt",
                hook_narration=hook_line, commentary_script=commentary,
                title="He Tried Outrunning 6-Star Leonida Police With Zero Armor #GTA6 #Shorts",
                description="Transformative breakdown of a high-speed GTA 6 Leonida police pursuit and physics escape.",
                hashtags=["#GTA6", "#GTAVI", "#GTA6Gameplay", "#GamingShorts", "#ViceCity", "#Reels"],
                caption_cues=cues,
            )
        )
        idx += 1
        cursor += 15.0
    candidates.sort(key=lambda c: c.viral_score, reverse=True)
    selected: List[ViralClipSpec] = []
    for cand in candidates:
        if len(selected) >= max_clips:
            break
        if not any(not (cand.end_sec <= ex.start_sec or cand.start_sec >= ex.end_sec) for ex in selected):
            selected.append(cand)
    return selected


def refine_hooks_with_llm(candidates: List[ViralClipSpec], segments: List[dict[str, Any]]) -> List[ViralClipSpec]:
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not gemini_key and not openai_key:
        return candidates
    for clip in candidates:
        excerpt = " ".join(s["text"] for s in segments if float(s["end"]) > clip.start_sec and float(s["start"]) < clip.end_sec)
        prompt = (
            f"Analyze this {clip.duration_sec:.1f}s GTA 6 stream segment ({clip.start_sec}s-{clip.end_sec}s): {excerpt}\n"
            'Return JSON with keys: "hook_narration", "commentary_script", "title", "description", "hashtags".'
        )
        response_json: Optional[dict[str, Any]] = None
        if gemini_key:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model="gemini-2.5-flash", contents=prompt, config={"response_mime_type": "application/json"}
                    )
                    if resp and resp.text:
                        response_json = json.loads(resp.text)
                        break
                except Exception as exc:
                    print(f"[Gemini Attempt {attempt + 1}/3] {exc}", file=sys.stderr)
                    if attempt < 2:
                        time.sleep(2 ** (attempt + 1))
        if response_json:
            clip.hook_narration = str(response_json.get("hook_narration") or clip.hook_narration)
            clip.commentary_script = str(response_json.get("commentary_script") or clip.commentary_script)
            clip.title = str(response_json.get("title") or clip.title)
            clip.description = str(response_json.get("description") or clip.description)
    return candidates


async def synthesize_voiceover_async(text: str, voice: str, audio_out: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text=text, voice=voice, rate="+8%").save(str(audio_out))


def generate_ass_subtitles(cues: List[WordCue], commentary_script: str, clip_duration: float, ass_path: Path) -> Path:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ViralPop,DejaVu Sans,68,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,2,90,150,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    words = [w.strip() for w in commentary_script.split() if w.strip()] or [c.word for c in cues]
    chunks = [words[i : i + 3] for i in range(0, len(words), 3)]
    active_window = min(clip_duration - 1.0, max(6.0, len(words) * 0.34))
    time_per_chunk = active_window / max(1, len(chunks))

    def fmt_time(sec: float) -> str:
        return f"{int(sec // 3600)}:{int((sec % 3600) // 60):02d}:{sec % 60:05.2f}"

    events = [
        f"Dialogue: 0,{fmt_time(i * time_per_chunk)},{fmt_time(min(clip_duration, (i + 1) * time_per_chunk))},ViralPop,,0,0,0,,{{\\fscx112\\fscy112\\t(0,120,\\fscx100\\fscy100)}}{' '.join(ch).upper()}"
        for i, ch in enumerate(chunks)
    ]
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass_path


def build_ffmpeg_faceless_command(
    raw_clip_path: Path, voiceover_path: Optional[Path], ass_path: Path,
    output_mp4_path: Path, duration_sec: float, layout_mode: str = "blur_fill_action"
) -> List[str]:
    ass_escaped = str(ass_path).replace("\\", "/").replace(":", r"\:")
    if layout_mode == "center_zoom_faceless":
        video_filter = f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos,fps=60,ass='{ass_escaped}'[vout]"
    else:
        video_filter = (
            "[0:v]split=2[bg_src][fg_src];"
            "[bg_src]crop=iw*0.72:ih:(iw*0.14):0,scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=28:4,eq=brightness=-0.12:saturation=1.15[bg];"
            "[fg_src]crop=iw*0.72:ih*0.90:(iw*0.14):(ih*0.05),scale=1080:1350:flags=lanczos[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,fps=60,ass='{ass_escaped}'[vout]"
        )
    audio_filter = (
        "[0:a]highpass=f=90,lowpass=f=7500,volume=0.22[game_a];"
        "[1:a]volume=1.45,apad[vo_a];"
        "[game_a][vo_a]amix=inputs=2:duration=first:dropout_transition=2,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )
    return [
        "ffmpeg", "-y", "-i", str(raw_clip_path), "-i", str(voiceover_path),
        "-t", f"{duration_sec:.2f}", "-filter_complex", f"{video_filter};{audio_filter}",
        "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium",
        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(output_mp4_path),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", type=str, default="")
    parser.add_argument("--max-clips", type=int, default=3)
    parser.add_argument("--min-duration", type=float, default=30.0)
    parser.add_argument("--max-duration", type=float, default=45.0)
    parser.add_argument("--layout", type=str, default="blur_fill_action")
    parser.add_argument("--voice", type=str, default="en-US-ChristopherNeural")
    parser.add_argument("--output-dir", type=str, default="output_shorts")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stream_url = args.url.strip() or os.environ.get("STREAM_URL", "").strip()
    if not stream_url:
        sys.exit("Error: Provide --url or STREAM_URL.")

    work_dir = out_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    meta = fetch_stream_metadata(stream_url, work_dir)
    audio_wav = download_audio_track(stream_url, work_dir)
    segments = transcribe_stream_audio(audio_wav)
    clips = score_candidate_windows_heuristic(segments, meta.get("heatmap"), args.min_duration, args.max_duration, args.max_clips)
    clips = refine_hooks_with_llm(clips, segments)

    for clip in clips:
        raw_mp4 = download_video_section(stream_url, clip.start_sec, clip.end_sec, work_dir / f"{clip.clip_id}_raw.mp4")
        vo_mp3 = work_dir / f"{clip.clip_id}_vo.mp3"
        asyncio.run(synthesize_voiceover_async(clip.commentary_script, args.voice, vo_mp3))
        ass_path = generate_ass_subtitles(clip.caption_cues, clip.commentary_script, clip.duration_sec, out_dir / f"{clip.clip_id}.ass")
        final_mp4 = out_dir / f"{clip.clip_id}_vertical_9x16.mp4"
        subprocess.run(build_ffmpeg_faceless_command(raw_mp4, vo_mp3, ass_path, final_mp4, clip.duration_sec, args.layout), check=True)

    (out_dir / "publishing_manifest.json").write_text(json.dumps([asdict(c) for c in clips], indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
