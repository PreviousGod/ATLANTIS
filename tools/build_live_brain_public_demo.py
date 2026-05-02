#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import generate_live_brain_demo_video as video
from live_brain_demo import DEFAULT_DEMO_DIR, seed_demo

DEMO_DIR = ROOT / 'demo'
FULL_VIDEO = DEMO_DIR / 'live_brain_control_room_demo.mp4'
FULL_VOICE_VIDEO = DEMO_DIR / 'live_brain_control_room_demo_voiceover.mp4'
TEASER_VIDEO = DEMO_DIR / 'live_brain_control_room_teaser.mp4'
THUMBNAIL = DEMO_DIR / 'live_brain_control_room_thumbnail.png'
FULL_TRANSCRIPT = DEMO_DIR / 'live_brain_voiceover_full.txt'
TEASER_TRANSCRIPT = DEMO_DIR / 'live_brain_voiceover_teaser.txt'
FULL_AUDIO = DEMO_DIR / 'live_brain_voiceover_full.wav'
TEASER_AUDIO = DEMO_DIR / 'live_brain_voiceover_teaser.wav'
PIPER_MODEL = Path('/home/deyaan666/.local/share/piper/en_US-ryan-medium.onnx')

FULL_SCRIPT = '''Most agent memory today is semantic search. It remembers similar text.
Useful, but not enough.
Live Brain maintains operational truth: what is verified, stale, blocked, risky, approved, and why it entered the prompt.
Here is the failure mode. Semantic search can find the old Enoch video because it looks similar.
Live Brain knows it was rejected, and selects the verified artifact instead.
It also keeps work state, not transcript soup: active, blocked, resolved, superseded.
When the agent learns from failures, high-risk changes do not silently mutate behavior.
They enter an approval gate with evidence, risk score, suggested tests, and an audit trail.
And when context enters the prompt, you can inspect why.
This is the missing control layer for long-running agents: verified artifacts, causal learning, context explainability, and safety-gated self-evolution.
Beyond vector memory. Live Brain makes agent memory operational.
'''

TEASER_SCRIPT = '''Semantic memory remembers text. Live Brain maintains operational truth.
It knows which artifact is verified, which one is stale, which work is active, and which self-change is risky.
The agent learns from failures, but high-risk changes go through an approval gate.
Beyond vector memory: auditable, safety-gated memory for long-running agents.
'''

TEASER_SCENES = [
    ('hero', 4.5, video.scene_hero),
    ('problem', 5.5, video.scene_problem),
    ('artifacts', 5.0, video.scene_artifacts),
    ('gate', 6.5, video.scene_gate),
    ('context', 4.5, video.scene_context),
    ('close', 4.0, video.scene_close),
]


def run(cmd: Sequence[str]) -> None:
    subprocess.run(list(cmd), check=True)


def duration_seconds(path: Path) -> float:
    with wave.open(str(path), 'rb') as handle:
        return handle.getnframes() / float(handle.getframerate())


def write_transcript(path: Path, text: str) -> None:
    path.write_text(text.strip() + '\n', encoding='utf-8')


def synthesize_voiceover(text_path: Path, wav_path: Path) -> None:
    piper = shutil.which('piper')
    model = Path(str(Path.cwd() / PIPER_MODEL)).resolve() if not PIPER_MODEL.is_absolute() else PIPER_MODEL
    config = Path(str(model) + '.json')
    if piper and model.exists():
        cmd = [
            piper,
            '-m', str(model),
            '-c', str(config),
            '-i', str(text_path),
            '-f', str(wav_path),
            '--length-scale', '0.97',
            '--sentence-silence', '0.24',
            '--volume', '0.95',
        ]
        run(cmd)
        return
    espeak = shutil.which('espeak-ng') or shutil.which('espeak')
    if espeak:
        run([espeak, '-v', 'en-us', '-s', '158', '-w', str(wav_path), text_path.read_text(encoding='utf-8')])
        return
    raise RuntimeError('No local TTS engine found. Install piper or espeak-ng.')


def mux_voiceover(video_in: Path, wav_in: Path, output: Path) -> None:
    run([
        'ffmpeg', '-y',
        '-i', str(video_in),
        '-i', str(wav_in),
        '-filter_complex', '[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,apad[a]',
        '-map', '0:v:0',
        '-map', '[a]',
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '160k',
        '-shortest',
        '-movflags', '+faststart',
        str(output),
    ])


def render_scenes(db_path: Path, output: Path, scenes, fps: int) -> None:
    original_scenes = video.SCENES
    video.SCENES = scenes
    workdir = output.parent / f'.{output.stem}_frames'
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        video.render_video(db_path, output, workdir, fps=fps)
    finally:
        video.SCENES = original_scenes
        shutil.rmtree(workdir, ignore_errors=True)


def render_thumbnail(db_path: Path, output: Path) -> None:
    data = video.load_demo_data(db_path)
    img = video.gradient_bg(18)
    draw = video.ImageDraw.Draw(img, 'RGBA')
    video.title_bar(img, 'Public Demo', 0)
    video.draw_text(draw, (105, 170), 'Beyond Vector Memory', video.f(88, bold=True), fill=video.TEXT)
    video.draw_text(draw, (110, 300), 'Operational memory for agents', video.f(56, bold=True), fill=video.CYAN)
    video.draw_text(
        draw,
        (114, 405),
        'Verified artifacts • work lifecycle • causal learning • gated self-evolution',
        video.f(31),
        fill=(205, 218, 246),
        max_width=980,
    )
    video.panel(img, (1120, 180, 1780, 830), 'Control Layer', glow=video.VIOLET)
    rows = [
        ('VERIFIED', 'enoch_part2_correct_final.mp4', video.GREEN),
        ('REJECTED', 'old wrong cut blocked', video.RED),
        ('ACTIVE', 'approval gate visible', video.CYAN),
        ('AUDIT', 'every learning event traced', video.YELLOW),
    ]
    y = 278
    for label, text, color in rows:
        video.rounded(draw, (1170, y, 1730, y + 96), 22, fill=(7, 12, 26, 185), outline=(255, 255, 255, 28), width=1)
        video.badge(draw, 1202, y + 25, label, color, small=True)
        video.draw_text(draw, (1388, y + 27), text, video.f(22, bold=True), fill=video.TEXT, max_width=295)
        y += 120
    video.badge(draw, 112, 635, 'SAFETY-GATED', video.GREEN)
    video.badge(draw, 410, 635, 'AUDITABLE', video.CYAN)
    video.badge(draw, 670, 635, 'SELF-EVOLVING', video.VIOLET)
    video.draw_text(draw, (112, 775), 'Semantic memory remembers text.\nLive Brain maintains operational truth.', video.f(39, bold=True), fill=video.TEXT, spacing=12)
    output.parent.mkdir(parents=True, exist_ok=True)
    img.convert('RGB').save(output, quality=96)


def build(args: argparse.Namespace) -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    db_path = DEFAULT_DEMO_DIR / 'live_brain.db'
    seed_demo(db_path, reset=args.reset_demo_db or not db_path.exists())

    write_transcript(FULL_TRANSCRIPT, FULL_SCRIPT)
    write_transcript(TEASER_TRANSCRIPT, TEASER_SCRIPT)

    if args.full:
        with tempfile.TemporaryDirectory(prefix='live_brain_full_') as tmp:
            video.render_video(db_path, FULL_VIDEO, Path(tmp), fps=args.fps)
        synthesize_voiceover(FULL_TRANSCRIPT, FULL_AUDIO)
        mux_voiceover(FULL_VIDEO, FULL_AUDIO, FULL_VOICE_VIDEO)
        print(f'Full voiceover: {FULL_VOICE_VIDEO} ({duration_seconds(FULL_AUDIO):.1f}s narration)')

    if args.teaser:
        render_scenes(db_path, TEASER_VIDEO, TEASER_SCENES, fps=args.fps)
        synthesize_voiceover(TEASER_TRANSCRIPT, TEASER_AUDIO)
        mux_voiceover(TEASER_VIDEO, TEASER_AUDIO, TEASER_VIDEO.with_name('live_brain_control_room_teaser_voiceover.mp4'))
        print(f'Teaser: {TEASER_VIDEO}')
        print(f'Teaser voiceover: {TEASER_VIDEO.with_name("live_brain_control_room_teaser_voiceover.mp4")} ({duration_seconds(TEASER_AUDIO):.1f}s narration)')

    if args.thumbnail:
        render_thumbnail(db_path, THUMBNAIL)
        print(f'Thumbnail: {THUMBNAIL}')

    print(f'Transcripts: {FULL_TRANSCRIPT}, {TEASER_TRANSCRIPT}')


def main() -> int:
    parser = argparse.ArgumentParser(description='Build public-grade Live Brain demo assets.')
    parser.add_argument('--fps', type=int, default=video.FPS)
    parser.add_argument('--reset-demo-db', action='store_true')
    parser.add_argument('--full', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--teaser', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--thumbnail', action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    build(args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
