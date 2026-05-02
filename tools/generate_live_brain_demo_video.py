#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from live_brain_demo import DEFAULT_DEMO_DIR, seed_demo

W, H = 1920, 1080
FPS = 24
BG = (7, 9, 18)
PANEL = (18, 25, 48)
PANEL2 = (10, 16, 32)
TEXT = (237, 244, 255)
MUTED = (148, 165, 204)
FAINT = (96, 115, 150)
CYAN = (99, 231, 255)
BLUE = (122, 162, 255)
VIOLET = (175, 122, 255)
GREEN = (116, 242, 167)
YELLOW = (255, 209, 102)
RED = (255, 107, 138)
ORANGE = (255, 159, 67)

FONT_DIRS = [
    Path('/usr/share/fonts/TTF'),
    Path('/usr/share/fonts/Adwaita'),
    Path('/usr/share/fonts/truetype/dejavu'),
    Path('/usr/share/fonts/truetype/liberation2'),
    Path('/usr/share/fonts/truetype/freefont'),
]


def font_path(name: str = 'DejaVuSans.ttf') -> str:
    for folder in FONT_DIRS:
        candidate = folder / name
        if candidate.exists():
            return str(candidate)
    for folder in FONT_DIRS:
        if folder.exists():
            for candidate in folder.glob('*.ttf'):
                return str(candidate)
    raise RuntimeError('No TTF font found')

FONT_REG = font_path('DejaVuSans.ttf')
FONT_BOLD = font_path('DejaVuSans-Bold.ttf')
FONT_MONO = font_path('DejaVuSansMono.ttf')


def f(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_MONO if mono else (FONT_BOLD if bold else FONT_REG), size)


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def load_demo_data(db: Path) -> Dict[str, Any]:
    with connect(db) as conn:
        artifacts = [dict(r) for r in conn.execute("SELECT project_key, role, path, label, status, confidence FROM verified_artifacts ORDER BY status='verified' DESC, role LIMIT 8")]
        work = [dict(r) for r in conn.execute("SELECT title, status, priority, next_step, root_cause FROM work_items ORDER BY priority DESC LIMIT 5")]
        proposal = dict(conn.execute("SELECT proposal_id, proposal_type, target_area, rationale, proposed_action, risk_level, risk_score, suggested_tests_json FROM self_evolution_proposals WHERE status='needs_approval' ORDER BY risk_score DESC LIMIT 1").fetchone())
        beliefs = [dict(r) for r in conn.execute("SELECT claim_text, belief_kind, status, confidence FROM beliefs ORDER BY status='validated' DESC, confidence DESC LIMIT 4")]
        rules = [dict(r) for r in conn.execute("SELECT category, action_json, confidence, times_confirmed FROM rules WHERE status='active' ORDER BY confidence DESC LIMIT 3")]
        timeline = [dict(r) for r in conn.execute("SELECT object_type, object_id, action, reason, created_at FROM audit_log ORDER BY created_at DESC LIMIT 6")]
        context = {
            'query': 'send me Enoch part 1 and part 2',
            'sections': ['MUST FOLLOW', 'VERIFIED ARTIFACTS', 'PROVEN FIX'],
            'lines': [
                'Use verified_artifacts before fuzzy search.',
                'project=enoch role=part_1 status=verified',
                'project=enoch role=part_2 status=verified',
            ],
        }
    return {'artifacts': artifacts, 'work': work, 'proposal': proposal, 'beliefs': beliefs, 'rules': rules, 'timeline': timeline, 'context': context}


def ease(x: float) -> float:
    return 1 - pow(1 - max(0, min(1, x)), 3)


def mix(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def lerp_color(c1, c2, t):
    return tuple(mix(c1[i], c2[i], t) for i in range(3))


def alpha(color, a):
    return (*color[:3], int(a))


def rounded(draw: ImageDraw.ImageDraw, xy, radius: int, fill, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def glow_rect(base: Image.Image, xy, radius: int, color, blur: int = 28, alpha_value: int = 80):
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(xy, radius=radius, fill=alpha(color, alpha_value))
    overlay = overlay.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(overlay)


_BASE_BG: Image.Image | None = None


def build_base_bg() -> Image.Image:
    img = Image.new('RGBA', (W, H), (*BG, 255))
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay, 'RGBA')
    od.ellipse((-260, -330, 900, 720), fill=(99, 231, 255, 42))
    od.ellipse((980, -360, 2300, 820), fill=(175, 122, 255, 34))
    od.ellipse((700, 660, 2100, 1450), fill=(48, 78, 160, 26))
    overlay = overlay.filter(ImageFilter.GaussianBlur(110))
    img.alpha_composite(overlay)
    d = ImageDraw.Draw(img, 'RGBA')
    for gx in range(0, W, 64):
        d.line((gx, 0, gx, H), fill=(255, 255, 255, 10), width=1)
    for gy in range(0, H, 64):
        d.line((0, gy, W, gy), fill=(255, 255, 255, 10), width=1)
    return img


def gradient_bg(frame: int) -> Image.Image:
    global _BASE_BG
    if _BASE_BG is None:
        _BASE_BG = build_base_bg()
    img = _BASE_BG.copy()
    d = ImageDraw.Draw(img, 'RGBA')
    pulse = int(18 + 14 * (0.5 + 0.5 * math.sin(frame / 42)))
    d.ellipse((W - 260, 90, W - 210, 140), fill=(99, 231, 255, pulse))
    d.ellipse((120, H - 180, 170, H - 130), fill=(175, 122, 255, pulse))
    return img


def text_size(draw, text, font):
    box = draw.textbbox((0,0), text, font=font)
    return box[2]-box[0], box[3]-box[1]


def draw_text(draw, xy, text, font, fill=TEXT, anchor=None, spacing=8, max_width=None):
    x, y = xy
    if max_width is None:
        draw.text((x, y), text, font=font, fill=fill, anchor=anchor, spacing=spacing)
        return
    words = text.split()
    lines = []
    cur = ''
    for word in words:
        test = (cur + ' ' + word).strip()
        if text_size(draw, test, font)[0] <= max_width or not cur:
            cur = test
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    draw.multiline_text((x, y), '\n'.join(lines), font=font, fill=fill, spacing=spacing)


def badge(draw, x, y, text, color=CYAN, small=False):
    ft = f(24 if not small else 19, bold=True)
    tw, th = text_size(draw, text, ft)
    pad_x, pad_y = (18, 9) if not small else (12, 6)
    rounded(draw, (x, y, x+tw+pad_x*2, y+th+pad_y*2), 999, fill=alpha(color, 34), outline=alpha(color, 110), width=2)
    draw.text((x+pad_x, y+pad_y-2), text, font=ft, fill=color)
    return x+tw+pad_x*2


def title_bar(img: Image.Image, scene: str, t: float):
    d = ImageDraw.Draw(img)
    rounded(d, (70, 54, 286, 116), 20, fill=(12, 18, 34, 210), outline=(255,255,255,35), width=2)
    badge(d, 92, 72, 'LIVE BRAIN', CYAN, small=True)
    draw_text(d, (320, 73), scene, f(25, bold=True), fill=(210,220,245))
    draw_text(d, (1700, 74), f'{int(t//60):02d}:{int(t%60):02d}', f(22, mono=True), fill=FAINT)


def panel(img, xy, title=None, glow=None):
    if glow:
        glow_rect(img, xy, 26, glow, blur=34, alpha_value=55)
    d = ImageDraw.Draw(img, 'RGBA')
    rounded(d, xy, 28, fill=(18,25,48,210), outline=(153,186,255,45), width=2)
    if title:
        draw_text(d, (xy[0]+28, xy[1]+22), title, f(27, bold=True), fill=TEXT)


def draw_metric(draw, x, y, value, label, color=CYAN):
    rounded(draw, (x, y, x+250, y+138), 24, fill=(18,25,48,220), outline=(153,186,255,40), width=2)
    draw.text((x+24, y+22), str(value), font=f(52, bold=True), fill=color)
    draw.text((x+24, y+88), label.upper(), font=f(18, bold=True), fill=MUTED)


def scene_hero(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Control Room', frame/FPS)
    draw_text(d, (110, 228), 'Semantic memory\nremembers text.', f(78, bold=True), fill=(225,235,255), spacing=18)
    draw_text(d, (110, 438), 'Live Brain maintains\noperational truth.', f(86, bold=True), fill=CYAN, spacing=18)
    draw_text(d, (116, 665), 'What is verified, stale, blocked, risky, approved — and why it entered the prompt.', f(32), fill=(198,211,240), max_width=900)
    panel(img, (1160, 218, 1760, 820), 'Autonomy Dial', glow=VIOLET)
    steps = [('Observe', 'record turns + tool evidence', GREEN), ('Learn', 'facts, beliefs, workflows', GREEN), ('Propose', 'self-evolution queue', GREEN), ('Safe Apply', 'low-risk only; high-risk gated', YELLOW)]
    y = 306
    for i, (name, note, col) in enumerate(steps):
        rounded(d, (1215, y, 1705, y+96), 22, fill=(7,12,26,175), outline=(255,255,255,24), width=1)
        d.ellipse((1240, y+36, 1260, y+56), fill=col)
        draw_text(d, (1285, y+20), name, f(27, bold=True), fill=TEXT)
        draw_text(d, (1285, y+56), note, f(20), fill=MUTED)
        y += 116
    badge(d, 110, 804, 'BEYOND VECTOR MEMORY', VIOLET)
    badge(d, 465, 804, 'SAFETY-GATED', GREEN)
    badge(d, 740, 804, 'AUDITABLE', CYAN)


def scene_problem(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'The Failure Mode', frame/FPS)
    draw_text(d, (100, 160), 'Vector search can find the wrong memory.', f(58, bold=True), fill=TEXT)
    draw_text(d, (104, 244), 'It retrieves similar text — not operational truth.', f(31), fill=MUTED)
    panel(img, (110, 350, 870, 820), 'Semantic search result', glow=RED)
    panel(img, (1050, 350, 1810, 820), 'Live Brain decision', glow=GREEN)
    # left card
    rounded(d, (160, 445, 820, 565), 20, fill=(255,107,138,30), outline=(255,107,138,95), width=2)
    draw_text(d, (190, 470), 'enoch_part2_old_wrong_cut.mp4', f(28, mono=True), fill=RED)
    badge(d, 190, 520, 'REJECTED', RED, small=True)
    draw_text(d, (190, 610), 'Looks similar. Wrong narration order.\nStill likely to appear in transcript search.', f(26), fill=(245,190,204), max_width=560)
    # right card
    rounded(d, (1100, 445, 1760, 565), 20, fill=(116,242,167,28), outline=(116,242,167,105), width=2)
    draw_text(d, (1130, 470), 'enoch_part2_correct_final.mp4', f(28, mono=True), fill=GREEN)
    badge(d, 1130, 520, 'VERIFIED', GREEN, small=True)
    draw_text(d, (1130, 610), 'Explicit role: project=enoch role=part_2.\nSafe to send. Old file is blocked.', f(26), fill=(205,255,222), max_width=560)
    draw_text(d, (730, 906), 'WOW: The agent does not just remember. It knows what is safe.', f(34, bold=True), fill=CYAN, anchor='mm')


def scene_artifacts(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Verified Artifacts', frame/FPS)
    draw_text(d, (100, 150), 'Project files become explicit truth, not fuzzy guesses.', f(52, bold=True), fill=TEXT, max_width=1200)
    panel(img, (100, 280, 1820, 860), 'Artifact Registry', glow=CYAN)
    headers = ['Project', 'Role', 'Status', 'Path']
    xs = [150, 360, 600, 820]
    for x, h in zip(xs, headers):
        draw_text(d, (x, 350), h.upper(), f(20, bold=True), fill=MUTED)
    y = 405
    for a in data['artifacts'][:5]:
        status = a['status']
        col = GREEN if status == 'verified' else RED if status == 'rejected' else YELLOW
        rounded(d, (135, y-18, 1785, y+64), 16, fill=(8,13,27,150), outline=(255,255,255,18), width=1)
        draw_text(d, (150, y), a['project_key'], f(24, bold=True), fill=TEXT)
        draw_text(d, (360, y), a['role'], f(24, mono=True), fill=(210,225,255))
        badge(d, 600, y-5, status.upper(), col, small=True)
        draw_text(d, (820, y), Path(a['path']).name, f(24, mono=True), fill=col if status != 'verified' else TEXT)
        y += 92
    draw_text(d, (150, 760), 'Rule: search_files finds candidates; verified_artifacts decides the winner.', f(31, bold=True), fill=CYAN)


def scene_work(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Work Graph', frame/FPS)
    draw_text(d, (100, 150), 'Not transcript soup — active state with lifecycle.', f(54, bold=True), fill=TEXT)
    colors = {'resolved': GREEN, 'active': CYAN, 'blocked': YELLOW}
    y = 285
    for w in data['work'][:3]:
        col = colors.get(w['status'], BLUE)
        panel(img, (120, y, 1800, y+165), None, glow=col)
        badge(d, 160, y+34, w['status'].upper(), col, small=True)
        draw_text(d, (340, y+28), w['title'], f(31, bold=True), fill=TEXT, max_width=900)
        draw_text(d, (340, y+78), 'Next: ' + w['next_step'], f(23), fill=MUTED, max_width=1000)
        draw_text(d, (1375, y+46), f"priority {w['priority']:.2f}", f(26, mono=True), fill=col)
        y += 190
    draw_text(d, (100, 900), 'Agent memory now has tasks: active, blocked, resolved, superseded.', f(33, bold=True), fill=CYAN)


def scene_gate(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Self-Evolution Gate', frame/FPS)
    draw_text(d, (100, 145), 'The agent can propose change. It cannot silently mutate itself.', f(51, bold=True), fill=TEXT, max_width=1360)
    prop = data['proposal']
    panel(img, (150, 275, 1770, 850), 'Pending Self-Evolution Proposal', glow=YELLOW)
    badge(d, 210, 350, prop['proposal_type'], CYAN)
    badge(d, 440, 350, 'target: ' + prop['target_area'], VIOLET)
    badge(d, 720, 350, f"risk: {prop['risk_level']} ({prop['risk_score']})", RED)
    draw_text(d, (210, 435), prop['proposal_id'], f(26, mono=True), fill=FAINT)
    draw_text(d, (210, 495), prop['rationale'], f(31, bold=True), fill=TEXT, max_width=1280)
    draw_text(d, (210, 620), prop['proposed_action'], f(29), fill=(210,225,250), max_width=1300)
    rounded(d, (210, 735, 460, 795), 16, fill=(116,242,167,42), outline=(116,242,167,140), width=2)
    draw_text(d, (260, 750), 'Approve', f(26, bold=True), fill=GREEN)
    rounded(d, (485, 735, 705, 795), 16, fill=(255,107,138,34), outline=(255,107,138,120), width=2)
    draw_text(d, (540, 750), 'Reject', f(26, bold=True), fill=RED)
    draw_text(d, (1020, 750), 'Human stays in control. Audit stays forever.', f(30, bold=True), fill=YELLOW)


def scene_context(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Why This Context?', frame/FPS)
    draw_text(d, (100, 145), 'Memory needs a debugger.', f(58, bold=True), fill=TEXT)
    draw_text(d, (104, 220), 'Inspect exactly why a fact, rule, or artifact entered the prompt.', f(31), fill=MUTED)
    panel(img, (120, 320, 1800, 850), 'Compiled Context', glow=BLUE)
    rounded(d, (170, 392, 1740, 458), 18, fill=(3,7,15,210), outline=(255,255,255,28), width=1)
    draw_text(d, (198, 410), '> ' + data['context']['query'], f(28, mono=True), fill=CYAN)
    y = 510
    for sec in data['context']['sections']:
        badge(d, 190, y, sec, VIOLET if sec == 'MUST FOLLOW' else CYAN, small=True)
        y += 58
    y = 510
    for line in data['context']['lines']:
        draw_text(d, (720, y+4), '• ' + line, f(27), fill=TEXT, max_width=900)
        y += 58
    draw_text(d, (190, 765), 'This turns memory from magic retrieval into inspectable infrastructure.', f(31, bold=True), fill=GREEN)


def scene_timeline(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Flight Recorder', frame/FPS)
    draw_text(d, (100, 145), 'Every learning event leaves a trail.', f(55, bold=True), fill=TEXT)
    panel(img, (150, 275, 1770, 880), 'Audit Timeline', glow=VIOLET)
    xline = 260
    d.line((xline, 360, xline, 800), fill=(99,231,255,110), width=3)
    y = 355
    events = data['timeline'][:5]
    if not events:
        events = [{'action': 'needs_approval', 'object_type': 'self_evolution_proposal', 'reason': 'Demo event'}]
    for i, e in enumerate(events):
        col = [CYAN, GREEN, YELLOW, VIOLET, BLUE][i % 5]
        d.ellipse((xline-13, y+5, xline+13, y+31), fill=col)
        draw_text(d, (310, y), f"{e.get('action','event')} · {e.get('object_type','object')}", f(28, bold=True), fill=TEXT)
        draw_text(d, (310, y+42), e.get('reason',''), f(22), fill=MUTED, max_width=1150)
        y += 94
    draw_text(d, (150, 922), 'Provenance is what makes agent learning trustworthy.', f(34, bold=True), fill=CYAN)


def scene_close(img, data, p, frame):
    d = ImageDraw.Draw(img, 'RGBA')
    title_bar(img, 'Launch Message', frame/FPS)
    draw_text(d, (130, 170), 'Beyond vector memory.', f(82, bold=True), fill=TEXT)
    draw_text(d, (130, 280), 'A safety-gated operational memory layer\nfor long-running agents.', f(64, bold=True), fill=CYAN, spacing=14)
    claims = ['Scope-aware truth', 'Verified artifacts', 'Work lifecycle', 'Causal learning', 'Context explainability', 'Audited self-evolution']
    x, y = 135, 515
    for i, c in enumerate(claims):
        col = [CYAN, GREEN, YELLOW, VIOLET, BLUE, ORANGE][i]
        x = badge(d, x, y, c, col) + 18
        if x > 1550:
            x = 135; y += 82
    draw_text(d, (130, 790), 'The agent learns — but the gate stays under human control.', f(42, bold=True), fill=TEXT)
    draw_text(d, (130, 880), 'Live Brain Control Room', f(32, mono=True), fill=MUTED)

SCENES = [
    ('hero', 7.0, scene_hero),
    ('problem', 8.0, scene_problem),
    ('artifacts', 8.0, scene_artifacts),
    ('work', 7.5, scene_work),
    ('gate', 9.0, scene_gate),
    ('context', 8.0, scene_context),
    ('timeline', 7.5, scene_timeline),
    ('close', 7.0, scene_close),
]


def render_frame(data, scene_idx: int, p: float, frame_no: int, global_t: float) -> Image.Image:
    img = gradient_bg(frame_no)
    name, duration, fn = SCENES[scene_idx]
    fn(img, data, ease(p), frame_no)
    # letterbox-safe progress bar
    d = ImageDraw.Draw(img, 'RGBA')
    total = sum(s[1] for s in SCENES)
    rounded(d, (120, H-64, W-120, H-54), 999, fill=(255,255,255,26))
    rounded(d, (120, H-64, 120 + int((W-240) * (global_t / total)), H-54), 999, fill=alpha(CYAN, 180))
    # fade in/out per scene
    fade = min(1, p / 0.11, (1 - p) / 0.11)
    if fade < 1:
        overlay = Image.new('RGBA', (W,H), (0,0,0,int((1-fade)*175)))
        img.alpha_composite(overlay)
    return img.convert('RGB')


def render_video(db_path: Path, output: Path, workdir: Path, fps: int = FPS) -> None:
    data = load_demo_data(db_path)
    frames = workdir / 'frames'
    frames.mkdir(parents=True, exist_ok=True)
    total_frames = int(sum(duration for _, duration, _ in SCENES) * fps)
    scene_starts = []
    acc = 0
    for idx, (_, duration, _) in enumerate(SCENES):
        scene_starts.append((acc, acc + int(duration * fps), idx, duration))
        acc += int(duration * fps)
    frame_no = 0
    for start, end, idx, duration in scene_starts:
        for local in range(end - start):
            p = local / max(1, end - start - 1)
            img = render_frame(data, idx, p, frame_no, frame_no / fps)
            img.save(frames / f'frame_{frame_no:05d}.jpg', quality=92, subsampling=0)
            frame_no += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'ffmpeg', '-y', '-framerate', str(fps), '-i', str(frames / 'frame_%05d.jpg'),
        '-f', 'lavfi', '-i', f'anullsrc=channel_layout=stereo:sample_rate=48000',
        '-shortest', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-crf', '18',
        '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(output)
    ]
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate a captioned Live Brain Control Room demo video.')
    parser.add_argument('--db', default='', help='Demo DB path. If omitted, seeds /tmp/live_brain_control_room_demo/live_brain.db.')
    parser.add_argument('--output', default=str(ROOT / 'demo' / 'live_brain_control_room_demo.mp4'))
    parser.add_argument('--reset-demo-db', action='store_true')
    parser.add_argument('--keep-frames', action='store_true')
    parser.add_argument('--fps', type=int, default=FPS)
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db).expanduser().resolve()
    else:
        db_path = DEFAULT_DEMO_DIR / 'live_brain.db'
        seed_demo(db_path, reset=args.reset_demo_db or not db_path.exists())
    output = Path(args.output).expanduser().resolve()
    workdir = output.parent / '.demo_video_frames'
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        render_video(db_path, output, workdir, fps=args.fps)
    finally:
        if not args.keep_frames:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f'Demo video written: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
