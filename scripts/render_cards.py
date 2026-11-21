#!/usr/bin/env python3
"""
Render the animated SVG cards for the GitHub profile README.

    python scripts/render_cards.py            # animated cards -> cards/
    STATIC=1 python scripts/render_cards.py   # final frame only (for screenshots)

Layout (top to bottom, gutters baked into each SVG so the README needs no spacing):
    hero · heatmap · activity · three stat tiles · section title (light/dark) · three repo cards

Data:   data/github.json  (written daily by .github/workflows/profile-cards.yml)
        If it is missing, a clearly-labelled sample dataset is used instead.
Fonts:  fonts/*.ttf, subset per card with pyftsubset and embedded as WOFF data URIs.
Output: cards/*.svg, the README repo row, and preview/index.html in STATIC mode.

Only things that work inside <img> are used: one inline <style> block with
CSS keyframes, SMIL <animate>/<set>, and data: URIs. No scripts, no external
resources. Everything is turned off under prefers-reduced-motion.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from xml.sax.saxutils import escape

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit here.
# ─────────────────────────────────────────────────────────────────────────────
CONFIG = {
    "login": "Sureshmathav2004",
    "name": "Suresh Mathav",
    "eyebrow": "MSc AI · Queen Mary / Full Stack · NexCell",
    "tagline": "MSc AI student at Queen Mary University of London, building ML-powered, full-stack products.",
    "site_label": "sureshmathav-inuk.vercel.app",
    "site_url": "https://sureshmathav-inuk.vercel.app",
    "location": "London, UK",
    "float_chips": ["{ } Python · FastAPI", "λ ML · React"],
    # Optional portrait: first file found is embedded (keep it small, ~560×720, <150 KB).
    "portrait_candidates": ["assets/portrait.jpg", "assets/portrait.jpeg", "assets/portrait.png", "assets/portrait.webp"],
    "monogram": "SM",
    # Heatmap title: plain part + highlighted part.
    "heatmap_title": ("Every day, ", "in neon"),
    # Three bold stat tiles (value, word, blurb, link). Keep blurbs to ~90 characters.
    "tiles": [
        {"id": "build", "label": "Build", "value": "4", "word": "internships",
         "blurb": "Full stack at NexCell now. Before that: Gilbert Research, Infilabs HealthTech, Salesforce.",
         "href": "https://www.linkedin.com/in/suresh-mathav", "fill": "violet"},
        {"id": "ship", "label": "Ship", "value": "3", "word": "live apps",
         "blurb": "Deployed on Render: a churn predictor, the BakaByte chat app and SmartML Prep.",
         "href": "https://github.com/Sureshmathav2004?tab=repositories", "fill": "brand"},
        {"id": "learn", "label": "Learn", "value": "MSc", "word": "in AI",
         "blurb": "At Queen Mary University of London, 2026–27. Azure and Google Cloud certified.",
         "href": "https://sureshmathav-inuk.vercel.app", "fill": "accent"},
    ],
    "section_title": ("What I'm ", "building"),
    # Footer links under the cards (text links in the README).
    "links": [("sureshmathav-inuk.vercel.app", "https://sureshmathav-inuk.vercel.app"),
              ("linkedin", "https://www.linkedin.com/in/suresh-mathav"),
              ("email", "mailto:sureshmathav123@gmail.com"),
              ("github", "https://github.com/Sureshmathav2004")],
    # Featured repos: list names to pin an order, or leave empty to use your pinned repos.
    "featured": [],
    "max_featured": 3,
    "colors": {
        # "London at night": deep indigo, hot magenta, amber and electric cyan
        "bg": "#0B0718",        # card background
        "surface": "#161028",   # raised panels, empty contribution cells
        "border": "#2A2142",
        "text": "#F7F3FF",
        "body": "#D6CFE8",
        "muted": "#9188AD",
        "ink": "#140C24",       # dark text on bright tiles and the light-mode section title
        "brand": "#FF3D8B",     # primary brand colour (heatmap, peak bar)
        "accent": "#FFB547",    # amber
        "accent2": "#2CE5D9",   # electric cyan
        "violet": "#7B5CFF",
    },
    "fonts": {  # role -> (file, css family, weight)
        "sans": ("fonts/FiraSans-Regular.ttf", "SM Sans", 400),
        "sans-semi": ("fonts/FiraSans-SemiBold.ttf", "SM Sans", 600),
        "mono": ("fonts/FiraCode-Medium.ttf", "SM Mono", 500),
        "mono-bold": ("fonts/FiraCode-Bold.ttf", "SM Mono", 700),
        "display": ("fonts/SpaceGrotesk-Bold.ttf", "SM Display", 700),
        "display-med": ("fonts/SpaceGrotesk-Medium.ttf", "SM Display", 500),
    },
    # Page timeline (seconds): each card starts at its offset so the page plays top to bottom.
    "timeline": {"hero": 0.0, "heatmap": 0.7, "activity": 1.2, "tiles": 1.7, "tile_step": 0.14,
                 "title": 2.2, "repos": 2.4, "repo_step": 0.14},
}

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "cards"
DATA = ROOT / "data" / "github.json"
STATIC = os.environ.get("STATIC", "").lower() in ("1", "true", "yes")
C = CONFIG["colors"]
W = 880.0          # outer width of a full-row card; README shows it at width="100%"
THIRD = W / 3      # outer width of a tile / repo card; README shows it at width="33.333%"
GUT = 6            # transparent margin on every side of every card (12px between neighbours)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ─────────────────────────────────────────────────────────────────────────────
# Fonts: metrics for layout, pyftsubset for embedding
# ─────────────────────────────────────────────────────────────────────────────
class Fonts:
    def __init__(self):
        from fontTools.ttLib import TTFont
        self.metrics = {}
        for role, (path, _, _) in CONFIG["fonts"].items():
            f = TTFont(ROOT / path, lazy=True)
            self.metrics[role] = (f.getBestCmap(), f["hmtx"].metrics, f["head"].unitsPerEm)
        self._cache: dict[tuple[str, str], str] = {}

    def width(self, text: str, role: str, size: float) -> float:
        cmap, hmtx, upem = self.metrics[role]
        total = 0
        for ch in text:
            g = cmap.get(ord(ch))
            total += hmtx[g][0] if g in hmtx else upem * 0.5
        return total * size / upem

    def fit(self, text: str, role: str, size: float, max_w: float) -> str:
        if self.width(text, role, size) <= max_w:
            return text
        while text and self.width(text + "…", role, size) > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    def wrap(self, text: str, role: str, size: float, max_w: float, max_lines: int) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if self.width(trial, role, size) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + " …", role, size, max_w)
        return [self.fit(l, role, size, max_w) for l in lines]

    def face(self, role: str, chars: str) -> str:
        """Subset `role` to `chars` with pyftsubset and return an @font-face rule."""
        path, family, weight = CONFIG["fonts"][role]
        chars = "".join(sorted(set(chars + " ")))
        key = (role, chars)
        if key not in self._cache:
            with tempfile.TemporaryDirectory() as tmp:
                txt, out = Path(tmp) / "t.txt", Path(tmp) / "o.woff"
                txt.write_text(chars, encoding="utf-8")
                cmd = [shutil.which("pyftsubset") or "pyftsubset", str(ROOT / path),
                       f"--text-file={txt}", f"--output-file={out}", "--flavor=woff",
                       "--layout-features=kern,tnum", "--no-hinting", "--desubroutinize",
                       "--name-IDs=1,2", "--drop-tables+=DSIG"]
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                except FileNotFoundError:  # pyftsubset not on PATH: same tool via the module
                    subprocess.run([sys.executable, "-m", "fontTools.subset", *cmd[1:]], check=True,
                                   capture_output=True)
                b64 = base64.b64encode(out.read_bytes()).decode()
            self._cache[key] = (f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
                                f"src:url(data:font/woff;base64,{b64}) format('woff')}}")
        return self._cache[key]


FONTS: Fonts  # set in main()


# ─────────────────────────────────────────────────────────────────────────────
# SVG building helpers
# ─────────────────────────────────────────────────────────────────────────────
class Card:
    """Collects markup + used glyphs, then wraps it with an inline <style> block.

    w, h are the drawable (inner) size; the SVG adds a transparent GUT margin on every side.
    """

    def __init__(self, outer_w: float, outer_h: float, title: str, t0: float = 0.0, gutter: float = GUT):
        self.ow, self.oh, self.g = outer_w, outer_h, gutter
        self.w, self.h = outer_w - 2 * gutter, outer_h - 2 * gutter
        self.title, self.t0 = title, t0
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.glyphs: dict[str, set] = {}

    # --- animation hooks -----------------------------------------------------
    def fx(self, kind: str, delay: float = 0.0, dur: float | None = None) -> str:
        """class/style attributes for a CSS keyframe animation (nothing in STATIC mode)."""
        if STATIC:
            return ""
        style = f"animation-delay:{self.t0 + delay:.2f}s" + (f";animation-duration:{dur:.2f}s" if dur else "")
        return f' class="a-{kind}" style="{style}"'

    def loop(self, kind: str, delay: float = 0.0, dur: float | None = None) -> str:
        """An endless ambient animation (spin, float, twinkle), off in STATIC and reduced-motion."""
        if STATIC:
            return ""
        return f' class="a-{kind}" style="animation-delay:{delay:.2f}s' + (f';animation-duration:{dur:.1f}s' if dur else "") + '"'

    def begin(self, delay: float) -> str:
        return f"{self.t0 + delay:.2f}s"

    def smil(self, animated: str, still: str) -> None:
        """SMIL can't be paused by CSS, so animated + final-frame copies are toggled by media query."""
        if STATIC:
            self.add(still)
        else:
            self.add(f'<g class="motion">{animated}</g><g class="still">{still}</g>')

    # --- primitives ----------------------------------------------------------
    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, role="sans", size=14, fill=None, anchor="start", extra="", fx="") -> str:
        self.glyphs.setdefault(role, set()).update(s)
        _, family, weight = CONFIG["fonts"][role]
        fam = {"SM Mono": "m", "SM Display": "d"}.get(family, "s")
        merged = re.match(r'\s*class="([^"]+)"(.*)', fx or "")
        if merged:  # one class attribute only: font class + animation class
            fam, fx = f"{fam} {merged.group(1)}", merged.group(2)
        return (f'<text x="{x:.1f}" y="{y:.1f}" class="{fam}" font-weight="{weight}" font-size="{size}" '
                f'fill="{fill or C["body"]}" text-anchor="{anchor}"{extra}{fx}>{escape(s)}</text>')

    def panel(self, fill: str | None = None, stroke: str | None = None, rx: float = 16) -> None:
        self.add(f'<rect x="0.5" y="0.5" width="{self.w - 1:.1f}" height="{self.h - 1:.1f}" rx="{rx}" '
                 f'fill="{fill or C["bg"]}" stroke="{stroke or C["border"]}"/>')

    def render(self) -> str:
        faces = "".join(FONTS.face(r, "".join(g)) for r, g in self.glyphs.items() if g)
        css = faces + BASE_CSS + ("" if STATIC else ANIM_CSS)
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        label = escape(self.title, {'"': "&quot;"})
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.ow:.3f}" height="{self.oh:.0f}" '
                f'viewBox="0 0 {self.ow:.3f} {self.oh:.0f}" role="img" aria-label="{label}">'
                f"<title>{escape(self.title)}</title><style>{css}</style>{defs}"
                f'<g transform="translate({self.g} {self.g})">{"".join(self.parts)}</g></svg>\n')


BASE_CSS = (".s{font-family:'SM Sans',-apple-system,'Segoe UI',sans-serif}"
            ".d{font-family:'SM Display','SM Sans',-apple-system,'Segoe UI',sans-serif}"
            ".m{font-family:'SM Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-ligatures:none}")

ANIM_CSS = """
.still{display:none}
.a-up,.a-fade,.a-grow,.a-wipe,.a-pop,.a-slide,.a-reveal,.a-scan{animation-fill-mode:both;animation-timing-function:cubic-bezier(.2,.7,.2,1)}
.a-up{animation-name:up;animation-duration:.8s}
.a-fade{animation-name:fade;animation-duration:.9s;animation-timing-function:ease-out}
.a-pop{animation-name:pop;animation-duration:.7s;transform-box:fill-box;transform-origin:center}
.a-grow{animation-name:grow;animation-duration:.9s;transform-box:fill-box;transform-origin:50% 100%}
.a-wipe{animation-name:wipe;animation-duration:1.1s;transform-box:fill-box;transform-origin:0 50%;animation-timing-function:cubic-bezier(.65,0,.25,1)}
.a-slide{animation-name:slide;animation-duration:.8s}
.a-reveal{animation-name:reveal;animation-duration:1.3s;transform-box:fill-box;transform-origin:50% 0;animation-timing-function:cubic-bezier(.65,0,.25,1)}
.a-scan{animation-name:scan;animation-duration:1.3s;animation-timing-function:cubic-bezier(.65,0,.25,1)}
.a-spin{animation:spin 30s linear infinite;transform-box:fill-box;transform-origin:center}
.a-float{animation:float 6s ease-in-out infinite;transform-box:fill-box}
.a-twinkle{animation:twinkle 3.2s ease-in-out infinite}
@keyframes up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes pop{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:none}}
@keyframes grow{from{transform:scaleY(0)}to{transform:none}}
@keyframes wipe{from{transform:scaleX(0)}to{transform:none}}
@keyframes slide{from{opacity:0;transform:translateX(-12px)}to{opacity:1;transform:none}}
@keyframes reveal{from{transform:scaleY(0)}to{transform:none}}
@keyframes scan{0%{opacity:0;transform:translateY(0)}12%{opacity:1}88%{opacity:1}100%{opacity:0;transform:translateY(var(--scan,300px))}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@keyframes twinkle{0%,100%{opacity:.25}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){
*{animation:none!important;transition:none!important}
.motion{display:none}.still{display:inline}
}
"""


def hex_mix(a: str, b: str, t: float) -> str:
    """Mix colour a toward b by t (0..1)."""
    a, b = a.lstrip("#"), b.lstrip("#")
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def luminance(hex_: str) -> float:
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def write(name: str, card: Card) -> None:
    (CARDS / name).write_text(card.render(), encoding="utf-8")
    print(f"  cards/{name}  ({(CARDS / name).stat().st_size / 1024:.1f} KB)")


def sparkle(c: Card, x: float, y: float, col: str, i: int, delay: float) -> None:
    c.add(f'<g{c.fx("fade", delay)}><path d="M{x - 5:.1f} {y:.1f}h10M{x:.1f} {y - 5:.1f}v10" stroke="{col}" '
          f'stroke-width="1.6" stroke-linecap="round"{c.loop("twinkle", i * 0.7)}/></g>')


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────
def load_data() -> tuple[dict, bool]:
    if DATA.exists():
        raw = json.loads(DATA.read_text(encoding="utf-8"))
        return raw.get("data", raw), False
    print("! data/github.json not found — rendering with SAMPLE data (cards are labelled).", file=sys.stderr)
    return sample_data(), True


def sample_data() -> dict:
    rnd = random.Random(7)
    end = dt.date.today()
    start = end - dt.timedelta(days=end.weekday() + 1 + 52 * 7)  # a Sunday ~1 year back
    weeks, day, week = [], start, []
    while day <= end:
        busy = 1.0 + 0.8 * ((day.month in (3, 4, 10, 11)) + (day.weekday() < 5))
        n = 0 if rnd.random() < 0.38 else int(rnd.expovariate(1 / (2.2 * busy)))
        week.append({"date": day.isoformat(), "contributionCount": n})
        if len(week) == 7:
            weeks.append({"contributionDays": week}); week = []
        day += dt.timedelta(days=1)
    if week:
        weeks.append({"contributionDays": week})
    total = sum(d["contributionCount"] for w in weeks for d in w["contributionDays"])
    repos = [
        {"name": "SmartML-Prep", "description": "Streamlit app that auto-detects the ML task and uses the Gemini API to suggest context-aware preprocessing steps.",
         "url": "https://github.com/Sureshmathav2004", "homepageUrl": "https://smartml-prep.onrender.com",
         "stargazerCount": 0, "forkCount": 0, "primaryLanguage": {"name": "Python", "color": "#3572A5"}},
        {"name": "BakaByte", "description": "Real-time community chat app with Flask, Socket.IO and SQLite, with email verification and password reset.",
         "url": "https://github.com/Sureshmathav2004", "homepageUrl": "https://bakabyte-the-community-app.onrender.com",
         "stargazerCount": 0, "forkCount": 0, "primaryLanguage": {"name": "HTML", "color": "#e34c26"}},
        {"name": "Customer-Churn-Prediction", "description": "Telecom churn models on 50,000 records (0.78 F1), served with Flask on Render.",
         "url": "https://github.com/Sureshmathav2004", "homepageUrl": "https://predicting-customer-churn-in-a-telecom.onrender.com",
         "stargazerCount": 0, "forkCount": 0, "primaryLanguage": {"name": "Jupyter Notebook", "color": "#DA5B0B"}},
    ]
    return {"user": {"name": CONFIG["name"], "login": CONFIG["login"],
                     "contributionsCollection": {"contributionCalendar": {"totalContributions": total, "weeks": weeks}},
                     "pinnedItems": {"nodes": repos}, "repositories": {"nodes": repos}}}


def calendar_days(user: dict) -> list[tuple[dt.date, int]]:
    weeks = user["contributionsCollection"]["contributionCalendar"]["weeks"]
    return [(dt.date.fromisoformat(d["date"]), d["contributionCount"]) for w in weeks for d in w["contributionDays"]]


def stats(days: list[tuple[dt.date, int]]) -> dict:
    counts = [c for _, c in days]
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    # current streak: today may still be empty, so allow starting from yesterday
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current += 1; i -= 1
    busiest = max(days, key=lambda d: (d[1], d[0])) if days else (dt.date.today(), 0)
    last = days[-1][0] if days else dt.date.today()
    months = OrderedDict()
    y, m = last.year, last.month
    for _ in range(12):
        months[(y, m)] = 0
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months = OrderedDict(reversed(list(months.items())))
    for d, c in days:
        if (d.year, d.month) in months:
            months[(d.year, d.month)] += c
    return {"total": sum(counts), "current": current, "longest": longest,
            "active": sum(1 for c in counts if c), "days": len(counts),
            "busiest": busiest, "months": months, "last": last}


def featured_repos(user: dict) -> list[dict]:
    own = CONFIG["login"].lower()  # never feature the profile README repo itself
    pool = {r["name"]: r for r in (user.get("repositories") or {}).get("nodes", []) if r and r["name"].lower() != own}
    pinned = [r for r in (user.get("pinnedItems") or {}).get("nodes", []) if r]
    pool.update({r["name"]: r for r in pinned})
    if CONFIG["featured"]:
        picked = [pool[n] for n in CONFIG["featured"] if n in pool]
        missing = [n for n in CONFIG["featured"] if n not in pool]
        if missing:
            print(f"! featured repos not found in data: {', '.join(missing)}", file=sys.stderr)
    elif pinned:
        picked = pinned
    else:
        picked = sorted(pool.values(), key=lambda r: -r.get("stargazerCount", 0))
    return picked[: CONFIG["max_featured"]]


def sample_badge(card: Card, is_sample: bool, color: str | None = None) -> None:
    if is_sample:
        card.add(card.text(card.w - 16, 18, "sample data", "mono", 10, color or C["accent"], "end", fx=card.fx("fade", 0.3)))


def days_word(n: int) -> str:
    return "day" if n == 1 else "days"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Hero
# ─────────────────────────────────────────────────────────────────────────────
def find_portrait() -> str | None:
    mime = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
    for rel in CONFIG["portrait_candidates"]:
        p = ROOT / rel
        if p.exists():
            if p.stat().st_size > 200_000:
                print(f"! {rel} is {p.stat().st_size // 1024} KB; resize it to keep the hero light.", file=sys.stderr)
            return f"data:image/{mime[p.suffix.lower()]};base64,{base64.b64encode(p.read_bytes()).decode()}"
    return None


def hero_card() -> Card:
    """Eyebrow, two-line name, tagline and link on the left; a neon-framed portrait on the right."""
    c = Card(W, 432, f'{CONFIG["name"]}: {CONFIG["tagline"]}', CONFIG["timeline"]["hero"])
    HW, HH = c.w, c.h
    first, _, last = CONFIG["name"].partition(" ")
    V = C["violet"]
    fw, fh = 272, 348
    fx0, fy0 = HW - 52 - fw, (HH - fh) / 2
    pcx, pcy = fx0 + fw / 2, fy0 + fh / 2

    c.defs.append(
        f'<clipPath id="card"><rect width="{HW}" height="{HH}" rx="16"/></clipPath>'
        f'<clipPath id="photo"><rect x="{fx0}" y="{fy0}" width="{fw}" height="{fh}" rx="22"/></clipPath>'
        f'<radialGradient id="g1" cx="{pcx / HW:.3f}" cy=".45" r=".55"><stop offset="0" stop-color="{C["brand"]}" stop-opacity=".42"/>'
        f'<stop offset=".45" stop-color="{V}" stop-opacity=".14"/><stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="g2" cx=".02" cy=".02" r=".6"><stop offset="0" stop-color="{V}" stop-opacity=".32"/>'
        f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="g3" cx=".18" cy="1.05" r=".45"><stop offset="0" stop-color="{C["accent2"]}" stop-opacity=".16"/>'
        f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="gridfade" cx=".3" cy=".5" r=".6"><stop offset="0" stop-color="#fff" stop-opacity=".9"/>'
        f'<stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>'
        f'<mask id="gridmask"><rect width="{HW}" height="{HH}" fill="url(#gridfade)"/></mask>'
        f'<pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" '
        f'stroke="{C["border"]}" stroke-width="1"/></pattern>'
        f'<pattern id="scanlines" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="4" height="1.2" fill="#000" opacity=".35"/></pattern>'
        f'<linearGradient id="edge" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["brand"]}"/>'
        f'<stop offset=".5" stop-color="{C["accent"]}"/><stop offset="1" stop-color="{C["accent2"]}"/></linearGradient>'
        f'<linearGradient id="fadeb" x1="0" y1="0" x2="0" y2="1"><stop offset=".55" stop-color="{C["bg"]}" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity=".88"/></linearGradient>'
        f'<linearGradient id="bar" x1="0" x2="1"><stop offset="0" stop-color="{C["brand"]}"/><stop offset="1" stop-color="{C["accent"]}"/></linearGradient>'
        f'<linearGradient id="namestill" gradientUnits="userSpaceOnUse" x1="48" y1="0" x2="420" y2="0">'
        f'<stop offset="0" stop-color="{C["brand"]}"/><stop offset=".6" stop-color="#FF6F6B"/><stop offset="1" stop-color="{C["accent"]}"/></linearGradient>'
        f'<linearGradient id="nameflow" gradientUnits="userSpaceOnUse" x1="48" y1="0" x2="420" y2="0" spreadMethod="reflect">'
        f'<stop offset="0" stop-color="{C["brand"]}"/><stop offset=".6" stop-color="#FF6F6B"/><stop offset="1" stop-color="{C["accent"]}"/>'
        f'<animateTransform attributeName="gradientTransform" type="translate" values="0 0;372 0;0 0" dur="9s" '
        f'begin="{c.begin(1.4)}" repeatCount="indefinite"/></linearGradient>'
        f'<filter id="blur18" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="18"/></filter>'
        f'<filter id="blur8" x="-30%" y="-60%" width="160%" height="220%"><feGaussianBlur stdDeviation="8"/></filter>'
    )

    # background: night glows, a fading tech grid, orbit rings, a hexagon and sparks
    c.panel(rx=16)
    c.add('<g clip-path="url(#card)">')
    c.add(f'<rect width="{HW}" height="{HH}" fill="url(#grid)" mask="url(#gridmask)" opacity=".7"{c.fx("fade", 0, 1.4)}/>')
    c.add(f'<rect width="{HW}" height="{HH}" fill="url(#g2)"{c.fx("fade", 0, 1.6)}/>')
    c.add(f'<rect width="{HW}" height="{HH}" fill="url(#g3)"{c.fx("fade", 0.2, 1.6)}/>')
    c.add(f'<rect width="{HW}" height="{HH}" fill="url(#g1)"{c.fx("fade", 0.1, 1.8)}/>')
    c.add(f'<g{c.fx("fade", 0.3, 1.5)}><circle cx="{pcx}" cy="{pcy}" r="214" fill="none" stroke="{C["brand"]}" stroke-opacity=".28" '
          f'stroke-width="1" stroke-dasharray="2 10"{c.loop("spin")}/>'
          f'<circle cx="{pcx}" cy="{pcy}" r="252" fill="none" stroke="{C["accent2"]}" stroke-opacity=".14" stroke-width="1"/></g>')
    hx, hy, hr = fx0 - 64, 70, 16
    hexpts = " ".join(f"{hx + hr * math.cos(math.radians(60 * k + 30)):.1f},{hy + hr * math.sin(math.radians(60 * k + 30)):.1f}"
                      for k in range(6))
    c.add(f'<g{c.fx("pop", 0.9)}><polygon points="{hexpts}" fill="none" stroke="{C["accent"]}" stroke-width="1.5" '
          f'stroke-opacity=".8"{c.loop("spin", 0, 14)}/></g>')
    for i, (sx, sy, col) in enumerate([(fx0 - 30, 22, C["accent2"]), (HW - 24, 22, C["accent"]),
                                        (fx0 - 100, HH - 30, C["brand"]), (HW - 26, HH - 14, C["accent2"])]):
        sparkle(c, sx, sy, col, i, 1.0 + i * 0.1)
    c.add("</g>")

    # left column
    ey = 70
    sq = f'<rect x="48" y="{ey - 9}" width="9" height="9" rx="2" fill="{C["accent2"]}"/>'
    c.smil(f'<g opacity="0"><set attributeName="opacity" to="1" begin="{c.begin(0.1)}"/>{sq}'
           f'<rect x="48" y="{ey - 9}" width="9" height="9" rx="2" fill="none" stroke="{C["accent2"]}">'
           f'<animate attributeName="stroke-opacity" values=".9;0" dur="2.2s" begin="{c.begin(1.2)}" repeatCount="indefinite"/>'
           f'<animate attributeName="stroke-width" values="1;9" dur="2.2s" begin="{c.begin(1.2)}" repeatCount="indefinite"/></rect></g>',
           sq)
    c.add(c.text(68, ey, CONFIG["eyebrow"].upper(), "mono", 11.5, C["body"], extra=' letter-spacing="1.6"',
                 fx=c.fx("slide", 0.12)))

    size = 92
    max_w = fx0 - 48 - 70
    while size > 56 and max(FONTS.width(first, "display", size), FONTS.width(last, "display", size)) > max_w:
        size -= 2
    y1 = 98 + size * 0.78
    y2 = y1 + size * 0.96
    c.add(c.text(44, y1, first, "display", size, C["text"], extra=' letter-spacing="-2.5"', fx=c.fx("up", 0.2)))
    glow = c.text(44, y2, last, "display", size, C["brand"], extra=' letter-spacing="-2.5" filter="url(#blur8)" opacity=".55"')
    c.add(f'<g{c.fx("fade", 0.5, 1.4)}>{glow}</g>')
    still = c.text(44, y2, last, "display", size, "url(#namestill)", extra=' letter-spacing="-2.5"')
    flow = c.text(44, y2, last, "display", size, "url(#nameflow)", extra=' letter-spacing="-2.5"')
    c.add(f'<g{c.fx("up", 0.32)}>')
    c.smil(flow, still)  # the gradient keeps flowing through the surname (SMIL gradientTransform)
    c.add("</g>")
    by = y2 + 26
    c.add(f'<rect x="48" y="{by:.0f}" width="72" height="4" rx="2" fill="url(#bar)"{c.fx("wipe", 0.75, 0.8)}/>')
    c.add(f'<rect x="128" y="{by:.0f}" width="10" height="4" rx="2" fill="{C["accent2"]}"{c.fx("pop", 1.05)}/>')

    lines = FONTS.wrap(CONFIG["tagline"], "sans", 17.5, max_w + 20, 2)
    ty = by + 42
    for i, ln in enumerate(lines):
        c.add(c.text(48, ty + i * 26, ln, "sans", 17.5, C["body"], fx=c.fx("up", 0.6 + i * 0.08)))
    ly = min(ty + len(lines) * 26 + 20, HH - 30)
    c.add(f'<g{c.fx("up", 0.85)}>' + c.text(48, ly, "↗", "mono-bold", 14, C["accent2"])
          + c.text(68, ly, CONFIG["site_label"], "mono", 13.5, C["accent2"]) + "</g>")

    # portrait: glow, offset outline, scan-reveal photo, HUD brackets, pills and floating chips
    portrait = find_portrait()
    c.add(f'<g{c.fx("fade", 0.25, 1.6)}><rect x="{fx0 - 6}" y="{fy0 + 8}" width="{fw + 12}" height="{fh}" rx="26" '
          f'fill="url(#edge)" opacity=".45" filter="url(#blur18)"/></g>')
    c.add(f'<g{c.fx("pop", 0.5, 1.0)}><rect x="{fx0 + 16}" y="{fy0 + 16}" width="{fw}" height="{fh}" rx="22" fill="none" '
          f'stroke="{C["accent2"]}" stroke-opacity=".55" stroke-width="1.2" stroke-dasharray="6 6"/></g>')
    photo = (f'<rect x="{fx0}" y="{fy0}" width="{fw}" height="{fh}" fill="{C["surface"]}"/>'
             + (f'<image href="{portrait}" x="{fx0}" y="{fy0}" width="{fw}" height="{fh}" preserveAspectRatio="xMidYMin slice"/>'
                if portrait else c.text(pcx, pcy + 30, CONFIG["monogram"], "display", 96, "url(#edge)", "middle"))
             + f'<rect x="{fx0}" y="{fy0}" width="{fw}" height="{fh}" fill="url(#scanlines)" opacity=".22"/>'
             + f'<rect x="{fx0}" y="{fy0}" width="{fw}" height="{fh}" fill="url(#fadeb)"/>')
    if STATIC:
        c.add(f'<g clip-path="url(#photo)">{photo}</g>')
    else:
        c.defs.append(f'<mask id="reveal" maskUnits="userSpaceOnUse"><rect x="{fx0 - 2}" y="{fy0 - 2}" width="{fw + 4}" '
                      f'height="{fh + 4}" fill="#fff"{c.fx("reveal", 0.35)}/></mask>')
        c.add(f'<g clip-path="url(#photo)"><g mask="url(#reveal)">{photo}</g></g>')
        c.add(f'<g opacity="0" class="a-scan" style="animation-delay:{c.t0 + 0.35:.2f}s;--scan:{fh}px">'
              f'<rect x="{fx0}" y="{fy0 - 1}" width="{fw}" height="3" fill="{C["accent2"]}"/>'
              f'<rect x="{fx0}" y="{fy0 - 8}" width="{fw}" height="16" fill="{C["accent2"]}" opacity=".5" filter="url(#blur8)"/></g>')
    c.add(f'<rect x="{fx0}" y="{fy0}" width="{fw}" height="{fh}" rx="22" fill="none" stroke="url(#edge)" stroke-width="2"'
          f'{c.fx("fade", 0.9)}/>')
    k, o = 20, 12
    corners = [(fx0 - o, fy0 - o, 1, 1), (fx0 + fw + o, fy0 - o, -1, 1),
               (fx0 - o, fy0 + fh + o, 1, -1), (fx0 + fw + o, fy0 + fh + o, -1, -1)]
    br = "".join(f'<path d="M{x} {y + sy * k}V{y}H{x + sx * k}"/>' for x, y, sx, sy in corners)
    c.add(f'<g fill="none" stroke="{C["text"]}" stroke-opacity=".85" stroke-width="2" stroke-linecap="round"'
          f'{c.fx("fade", 1.05)}>{br}</g>')
    lx, lyy = fx0 + 16, fy0 + fh - 22
    lw = FONTS.width(CONFIG["location"], "mono", 12) + 34
    c.add(f'<g{c.fx("up", 1.15)}><rect x="{lx}" y="{lyy - 17}" width="{lw:.0f}" height="26" rx="13" fill="{C["bg"]}" '
          f'fill-opacity=".72" stroke="{C["text"]}" stroke-opacity=".18"/>'
          + c.text(lx + 26, lyy, CONFIG["location"], "mono", 12, C["text"]) + "</g>")
    dot = f'<circle cx="{lx + 14}" cy="{lyy - 4}" r="4" fill="{C["accent2"]}"/>'
    c.smil(f'<g opacity="0"><set attributeName="opacity" to="1" begin="{c.begin(1.2)}"/>{dot}'
           f'<circle cx="{lx + 14}" cy="{lyy - 4}" r="4" fill="none" stroke="{C["accent2"]}">'
           f'<animate attributeName="r" values="4;11" dur="2s" begin="{c.begin(1.4)}" repeatCount="indefinite"/>'
           f'<animate attributeName="stroke-opacity" values=".9;0" dur="2s" begin="{c.begin(1.4)}" repeatCount="indefinite"/></circle></g>',
           dot)
    spots = [(fx0 - 56, fy0 + 214, C["brand"]), (fx0 + fw - 150, fy0 - 16, C["accent"])]
    for i, (label, (cx_, cy_, col)) in enumerate(zip(CONFIG["float_chips"], spots)):
        cw = FONTS.width(label, "mono-bold", 12) + 26
        cx_ = min(cx_, HW - 14 - cw)
        c.add(f'<g{c.fx("pop", 1.25 + i * 0.15)}><g{c.loop("float", 1.8 + i * 1.3)}>'
              f'<rect x="{cx_:.0f}" y="{cy_}" width="{cw:.0f}" height="30" rx="9" fill="{C["surface"]}" stroke="{col}" stroke-width="1.3"/>'
              f'<rect x="{cx_:.0f}" y="{cy_}" width="{cw:.0f}" height="30" rx="9" fill="{col}" opacity=".08"/>'
              + c.text(cx_ + cw / 2, cy_ + 19.5, label, "mono-bold", 12, C["text"], "middle") + "</g></g>")
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 2. Heatmap
# ─────────────────────────────────────────────────────────────────────────────
def heatmap_card(user: dict, s: dict, is_sample: bool) -> Card:
    weeks = user["contributionsCollection"]["contributionCalendar"]["weeks"][-53:]
    px, gy = 40, 78
    step = (W - 2 * GUT - 2 * px) / len(weeks)
    cell = step - 3
    c = Card(W, gy + 7 * step + 50 + 2 * GUT, f'Contribution graph: {s["total"]:,} contributions in the last year, refreshed daily',
             CONFIG["timeline"]["heatmap"])
    legend_x = c.w - px - 5 * (cell + 3) - FONTS.width("more", "mono", 10.5) - 8 - FONTS.width("less", "mono", 10.5) - 26
    c.defs.append(f'<linearGradient id="hl" x1="0" x2="1"><stop offset="0" stop-color="{C["brand"]}"/>'
                  f'<stop offset="1" stop-color="{C["accent"]}"/></linearGradient>'
                  f'<radialGradient id="hg" cx=".85" cy="0" r=".7"><stop offset="0" stop-color="{C["brand"]}" stop-opacity=".16"/>'
                  f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></radialGradient>'
                  f'<clipPath id="hc"><rect width="{c.w}" height="{c.h}" rx="16"/></clipPath>')
    nonzero = sorted(d["contributionCount"] for w in weeks for d in w["contributionDays"] if d["contributionCount"])
    q = [nonzero[int(len(nonzero) * p)] if nonzero else 1 for p in (0.25, 0.5, 0.75)]
    shades = [C["surface"]] + [hex_mix(C["bg"], C["brand"], t) for t in (0.32, 0.55, 0.78, 1.0)]

    def level(n: int) -> int:
        return 0 if n == 0 else 1 if n <= q[0] else 2 if n <= q[1] else 3 if n <= q[2] else 4

    c.add(f'<g{c.fx("fade", 0, .6)}>')
    c.panel()
    c.add(f'<rect width="{c.w}" height="{c.h}" fill="url(#hg)" clip-path="url(#hc)"/></g>')
    plain, hi = CONFIG["heatmap_title"]
    pw = FONTS.width(plain, "display-med", 30)
    c.add(f'<g{c.fx("slide", 0.05)}>' + c.text(px, 50, plain, "display-med", 30, C["text"])
          + c.text(px + pw, 50, hi, "display", 30, "url(#hl)") + "</g>")
    c.add(c.text(c.w - px, 46, "REFRESHED DAILY", "mono", 11, C["muted"], "end", ' letter-spacing="1.8"', fx=c.fx("fade", 0.2)))
    last_month, today = None, None
    for wi, w in enumerate(weeks):
        days = w["contributionDays"]
        first = dt.date.fromisoformat(days[0]["date"])
        x = px + wi * step
        if first.month != last_month:
            if last_month is not None and x + 26 < legend_x:
                c.add(c.text(x, gy + 7 * step + 18, MONTHS[first.month - 1], "mono", 10.5, C["muted"],
                             fx=c.fx("fade", 0.3 + wi * 0.012)))
            last_month = first.month
        col = [f'<g{c.fx("pop", 0.15 + wi * 0.016, 0.5)}>']
        for d in days:
            dd = dt.date.fromisoformat(d["date"])
            row = (dd.weekday() + 1) % 7  # Sunday first, like GitHub
            col.append(f'<rect x="{x:.1f}" y="{gy + row * step:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="3" '
                       f'fill="{shades[level(d["contributionCount"])]}"/>')
            today = (x, gy + row * step)
        col.append("</g>")
        c.add("".join(col))
    # today's cell: a cyan ring that keeps pinging (SMIL)
    if today:
        tx, ty_ = today[0] + cell / 2, today[1] + cell / 2
        ring = f'<rect x="{today[0] - 2:.1f}" y="{today[1] - 2:.1f}" width="{cell + 4:.1f}" height="{cell + 4:.1f}" rx="4" fill="none" stroke="{C["accent2"]}" stroke-width="1.5"/>'
        c.smil(f'<g opacity="0"><set attributeName="opacity" to="1" begin="{c.begin(1.2)}"/>{ring}'
               f'<circle cx="{tx:.1f}" cy="{ty_:.1f}" r="6" fill="none" stroke="{C["accent2"]}">'
               f'<animate attributeName="r" values="6;16" dur="2.2s" begin="{c.begin(1.4)}" repeatCount="indefinite"/>'
               f'<animate attributeName="stroke-opacity" values=".8;0" dur="2.2s" begin="{c.begin(1.4)}" repeatCount="indefinite"/></circle></g>',
               ring)
    # legend
    ly = gy + 7 * step + 18
    lx = c.w - px - 5 * (cell + 3) - FONTS.width("more", "mono", 10.5) - 8
    c.add(f'<g{c.fx("fade", 1.0)}>' + c.text(lx - 8, ly, "less", "mono", 10.5, C["muted"], "end")
          + "".join(f'<rect x="{lx + i * (cell + 3):.1f}" y="{ly - cell + 1:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="3" fill="{col}"/>'
                    for i, col in enumerate(shades))
          + c.text(lx + 5 * (cell + 3) + 5, ly, "more", "mono", 10.5, C["muted"]) + "</g>")
    sample_badge(c, is_sample)
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 3. Activity
# ─────────────────────────────────────────────────────────────────────────────
def activity_card(s: dict, is_sample: bool) -> Card:
    b = s["busiest"]
    c = Card(W, 352, f'{s["total"]:,} contributions in the last 12 months; current streak {s["current"]} days, '
                     f'{s["active"]} of {s["days"]} days with a commit, {b[1]} on the busiest day', CONFIG["timeline"]["activity"])
    c.defs.append(f'<linearGradient id="bar" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C["violet"]}"/>'
                  f'<stop offset="1" stop-color="{C["violet"]}" stop-opacity=".35"/></linearGradient>'
                  f'<linearGradient id="peak" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C["accent"]}"/>'
                  f'<stop offset="1" stop-color="{C["brand"]}"/></linearGradient>'
                  f'<linearGradient id="hl" x1="0" x2="1"><stop offset="0" stop-color="{C["brand"]}"/>'
                  f'<stop offset="1" stop-color="{C["accent"]}"/></linearGradient>'
                  f'<filter id="glow" x="-60%" y="-30%" width="220%" height="160%"><feGaussianBlur stdDeviation="9"/></filter>'
                  f'<radialGradient id="ag" cx=".1" cy="1" r=".6"><stop offset="0" stop-color="{C["violet"]}" stop-opacity=".18"/>'
                  f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></radialGradient>'
                  f'<clipPath id="ac"><rect width="{c.w}" height="{c.h}" rx="16"/></clipPath>')
    c.add(f'<g{c.fx("fade", 0, .6)}>')
    c.panel()
    c.add(f'<rect width="{c.w}" height="{c.h}" fill="url(#ag)" clip-path="url(#ac)"/></g>')
    px = 40
    c.add(c.text(px, 58, "THE LAST 12 MONTHS ON GITHUB", "mono", 11, C["muted"], extra=' letter-spacing="1.8"',
                 fx=c.fx("slide", 0.05)))
    num = f'{s["total"]:,}'
    nw = FONTS.width(num, "display", 46)
    c.add(f'<g{c.fx("up", 0.12)}>' + c.text(px - 2, 108, num, "display", 46, C["text"], extra=' letter-spacing="-1"')
          + c.text(px + nw + 10, 108, "contributions", "display-med", 46, "url(#hl)", extra=' letter-spacing="-1"') + "</g>")

    # bars
    x0, x1 = px, 560
    top, base = 150, 300
    n = len(s["months"])
    slot = (x1 - x0) / n
    bw = slot * 0.72
    vals = list(s["months"].values())
    peak = max(max(vals), 1)
    pi = vals.index(peak)
    for i, ((y, m), v) in enumerate(s["months"].items()):
        cx = x0 + slot * i + slot / 2
        bh = max((base - top) * v / peak, 3)
        current = i == n - 1
        fill = "url(#peak)" if i == pi else (hex_mix(C["surface"], C["muted"], .35) if current else "url(#bar)")
        d = 0.35 + i * 0.05
        if i == pi:
            c.add(f'<rect x="{cx - bw / 2:.1f}" y="{base - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="5" fill="{C["brand"]}" '
                  f'opacity=".55" filter="url(#glow)"{c.fx("fade", d + 0.4)}/>')
            c.add(c.text(cx, base - bh - 10, f"{v:,}", "mono-bold", 11.5, C["accent"], "middle", fx=c.fx("fade", d + 0.5)))
        c.add(f'<rect x="{cx - bw / 2:.1f}" y="{base - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="5" fill="{fill}"'
              f'{c.fx("grow", d)}/>')
        c.add(c.text(cx, base + 22, MONTHS[m - 1], "mono", 11, C["muted"], "middle", fx=c.fx("fade", 0.3 + i * 0.03)))

    # stats column
    sx, sw = 610, c.w - 610 - px
    longest_note = "current streak, and the longest" if s["current"] and s["current"] >= s["longest"] \
        else f'current streak · longest {s["longest"]}'
    rows = [
        (f'{s["current"]}', days_word(s["current"]), longest_note, C["accent2"]),
        (f'{s["active"]}', f'/ {s["days"]}', "days with a commit", C["violet"]),
        (f'{b[1]}', "", f'on the busiest day · {b[0].day} {MONTHS[b[0].month - 1]}', C["accent"]),
    ]
    for i, (big, small, label, col) in enumerate(rows):
        y = 74 + i * 92
        bwid = FONTS.width(big, "display", 36)
        c.add(f'<g{c.fx("up", 0.45 + i * 0.12)}>'
              + f'<rect x="{sx}" y="{y - 26}" width="3" height="30" rx="1.5" fill="{col}"/>'
              + c.text(sx + 16, y, big, "display", 36, C["text"], extra=' letter-spacing="-.5"')
              + (c.text(sx + 16 + bwid + 8, y, small, "display-med", 30, C["muted"]) if small else "")
              + c.text(sx + 16, y + 26, label, "sans", 13.5, C["body"]) + "</g>")
        if i < 2:
            c.add(f'<line x1="{sx}" y1="{y + 52}" x2="{sx + sw}" y2="{y + 52}" stroke="{C["border"]}"{c.fx("wipe", 0.6 + i * 0.12)}/>')
    # live pulse beside the streak when it is running (SMIL)
    if s["current"]:
        px_ = sx + 16 + FONTS.width(rows[0][0], "display", 36) + FONTS.width(rows[0][1], "display-med", 30) + 24
        dot = f'<circle cx="{px_:.1f}" cy="62" r="4" fill="{C["accent2"]}"/>'
        c.smil(f'<g opacity="0"><set attributeName="opacity" to="1" begin="{c.begin(0.7)}"/>{dot}'
               f'<circle cx="{px_:.1f}" cy="62" r="4" fill="none" stroke="{C["accent2"]}">'
               f'<animate attributeName="r" values="4;12" dur="2s" begin="{c.begin(0.9)}" repeatCount="indefinite"/>'
               f'<animate attributeName="stroke-opacity" values=".9;0" dur="2s" begin="{c.begin(0.9)}" repeatCount="indefinite"/></circle></g>',
               dot)
    sample_badge(c, is_sample)
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 4. Stat tiles (three in a row)
# ─────────────────────────────────────────────────────────────────────────────
def tile_card(tile: dict, idx: int) -> Card:
    tl = CONFIG["timeline"]
    c = Card(THIRD, 222, f'{tile["label"]}: {tile["value"]} {tile["word"]}. {tile["blurb"]}', tl["tiles"] + idx * tl["tile_step"])
    base = C[tile["fill"]]
    dark_text = luminance(base) > 0.55
    ink = C["ink"] if dark_text else "#FFFFFF"
    sub = hex_mix(base, ink, 0.82)
    pad = 24
    c.defs.append(f'<linearGradient id="tf" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{hex_mix(base, "#FFFFFF", .12)}"/>'
                  f'<stop offset="1" stop-color="{hex_mix(base, C["bg"], .22)}"/></linearGradient>'
                  f'<clipPath id="tc"><rect width="{c.w:.1f}" height="{c.h}" rx="16"/></clipPath>')
    c.add(f'<g{c.fx("up", 0)}>')
    c.add(f'<rect width="{c.w:.1f}" height="{c.h}" rx="16" fill="url(#tf)"/>')
    # decorative geometry, one shape per tile, slowly turning in the corner
    shapes = [
        lambda cx, cy: f'<circle cx="{cx}" cy="{cy}" r="74" fill="none" stroke="{ink}" stroke-opacity=".16" stroke-width="16" stroke-dasharray="8 14"/>',
        lambda cx, cy: '<polygon points="' + " ".join(f"{cx + 82 * math.cos(math.radians(60 * k)):.1f},{cy + 82 * math.sin(math.radians(60 * k)):.1f}" for k in range(6))
                       + f'" fill="none" stroke="{ink}" stroke-opacity=".2" stroke-width="2"/>',
        lambda cx, cy: '<polygon points="' + " ".join(f"{cx + 86 * math.cos(math.radians(120 * k - 90)):.1f},{cy + 86 * math.sin(math.radians(120 * k - 90)):.1f}" for k in range(3))
                       + f'" fill="none" stroke="{ink}" stroke-opacity=".22" stroke-width="2"/>',
    ]
    sx, sy = c.w - 4, c.h + 6
    c.add(f'<g clip-path="url(#tc)"><g{c.fx("fade", 0.3, 1.2)}><g{c.loop("spin", 0, 40 + idx * 8)}>{shapes[idx % 3](sx, sy)}</g></g>'
          f'<circle cx="{sx}" cy="{sy}" r="36" fill="{ink}" opacity=".08"/></g>')
    c.add(c.text(pad, 40, tile["label"].upper(), "mono-bold", 11.5, sub, extra=' letter-spacing="2"'))
    c.add(c.text(c.w - pad, 41, "↗", "mono-bold", 15, ink, "end", fx=c.fx("pop", 0.45)))
    size = 46
    while size > 28 and FONTS.width(tile["value"] + " ", "display", size) + FONTS.width(tile["word"], "display-med", size * 0.72) > c.w - 2 * pad:
        size -= 2
    vw = FONTS.width(tile["value"] + " ", "display", size)
    c.add(f'<g{c.fx("up", 0.15)}>' + c.text(pad - 1, 108, tile["value"], "display", size, ink, extra=' letter-spacing="-1"')
          + c.text(pad + vw, 108, tile["word"], "display-med", round(size * 0.72), ink, extra=' letter-spacing="-.5"') + "</g>")
    for i, ln in enumerate(FONTS.wrap(tile["blurb"], "sans", 14.5, c.w - 2 * pad, 3)):
        c.add(c.text(pad, 146 + i * 21, ln, "sans", 14.5, sub, fx=c.fx("up", 0.28 + i * 0.06)))
    c.add("</g>")
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 5. Section title (light + dark variants, served through <picture>)
# ─────────────────────────────────────────────────────────────────────────────
def title_card(dark: bool) -> Card:
    plain, hi = CONFIG["section_title"]
    c = Card(W, 64, plain + hi, CONFIG["timeline"]["title"], gutter=0)
    c.defs.append(f'<linearGradient id="hl" x1="0" x2="1"><stop offset="0" stop-color="{C["brand"]}"/>'
                  f'<stop offset="1" stop-color="{hex_mix(C["accent"], C["ink"], 0 if dark else .25)}"/></linearGradient>')
    ink = C["text"] if dark else C["ink"]
    pw = FONTS.width(plain, "display-med", 32)
    hw = FONTS.width(hi, "display", 32)
    c.add(f'<g{c.fx("slide", 0)}>' + c.text(GUT + 2, 46, plain, "display-med", 32, ink)
          + c.text(GUT + 2 + pw, 46, hi, "display", 32, "url(#hl)") + "</g>")
    c.add(f'<rect x="{GUT + 2 + pw + hw + 12:.1f}" y="34" width="12" height="12" rx="3" fill="{C["accent2"]}"{c.fx("pop", 0.35)}/>')
    c.add(f'<rect x="{GUT + 2 + pw + hw + 32:.1f}" y="39.5" width="{W - (GUT + 2 + pw + hw + 32) - GUT:.1f}" height="1" '
          f'fill="{C["muted"] if dark else C["ink"]}" opacity="{.5 if dark else .3}"{c.fx("wipe", 0.3)}/>')
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 6. Repo cards (three in a row)
# ─────────────────────────────────────────────────────────────────────────────
def repo_card(r: dict, idx: int, is_sample: bool) -> Card:
    tl = CONFIG["timeline"]
    c = Card(THIRD, 196, f'{r["name"]}: {r.get("description") or ""}', tl["repos"] + idx * tl["repo_step"])
    lang = r.get("primaryLanguage") or {}
    accent = [C["violet"], C["brand"], C["accent"]][idx % 3]
    pad = 22
    c.defs.append(f'<radialGradient id="rg" cx="1" cy="0" r=".9"><stop offset="0" stop-color="{accent}" stop-opacity=".2"/>'
                  f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></radialGradient>'
                  f'<clipPath id="rc"><rect width="{c.w:.1f}" height="{c.h}" rx="16"/></clipPath>')
    c.add(f'<g{c.fx("up", 0)}>')
    c.panel()
    c.add(f'<rect width="{c.w:.1f}" height="{c.h}" fill="url(#rg)" clip-path="url(#rc)"/>')
    c.add(f'<rect x="{pad}" y="27" width="10" height="10" rx="2.5" fill="{accent}"{c.fx("pop", 0.2)}/>')
    ns = 18
    while ns > 14.5 and FONTS.width(r["name"], "display", ns) > c.w - 2 * pad - 24:
        ns -= 0.5
    name = FONTS.fit(r["name"], "display", ns, c.w - 2 * pad - 24)
    c.add(c.text(pad + 20, 37, name, "display", ns, C["text"], fx=c.fx("slide", 0.1)))
    desc = r.get("description") or "No description yet."
    for i, ln in enumerate(FONTS.wrap(desc, "sans", 14, c.w - 2 * pad, 3)):
        c.add(c.text(pad, 72 + i * 21, ln, "sans", 14, C["body"], fx=c.fx("up", 0.2 + i * 0.06)))
    bits = [lang.get("name")] if lang.get("name") else []
    if r.get("stargazerCount"):
        bits.append(f'★ {r["stargazerCount"]}')
    if r.get("homepageUrl"):
        bits.append("live")
    fy = c.h - 26
    c.add(f'<line x1="{pad}" y1="{fy - 22}" x2="{c.w - pad:.1f}" y2="{fy - 22}" stroke="{C["border"]}"{c.fx("wipe", 0.3)}/>')
    if lang.get("color"):
        c.add(f'<circle cx="{pad + 4}" cy="{fy - 4}" r="4" fill="{lang["color"]}"/>')
    c.add(c.text(pad + (14 if lang.get("color") else 0), fy, "  ·  ".join(bits), "mono", 11.5, C["muted"], fx=c.fx("fade", 0.4)))
    c.add(c.text(c.w - pad, fy, "↗", "mono-bold", 13, accent, "end", fx=c.fx("pop", 0.5)))
    c.add("</g>")
    sample_badge(c, is_sample)
    return c


# ─────────────────────────────────────────────────────────────────────────────
# README + static preview
# ─────────────────────────────────────────────────────────────────────────────
def update_readme(repos: list[tuple[str, dict]]) -> None:
    readme = ROOT / "README.md"
    if not readme.exists():
        return
    width = f"{100 / 3:.3f}%"
    row = "".join(f'<a href="{r["url"]}"><img src="./cards/{f}" width="{width}" alt="{escape(r["name"], {chr(34): "&quot;"})}" /></a>'
                  for f, r in repos)
    text = readme.read_text(encoding="utf-8")
    new = re.sub(r"<!-- cards:repos:start -->.*?<!-- cards:repos:end -->",
                 lambda _: f"<!-- cards:repos:start -->\n{row}\n<!-- cards:repos:end -->", text, flags=re.S)
    if new != text:
        readme.write_text(new, encoding="utf-8")
        print("  README.md repo row updated")


def write_preview() -> None:
    """GitHub-like page for screenshots: STATIC=1 python scripts/render_cards.py, then open preview/index.html."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    body = readme.replace('src="./cards/', 'src="../cards/').replace('srcset="./cards/', 'srcset="../cards/')
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    out = ROOT / "preview"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><meta name=color-scheme content='dark light'><title>Profile preview</title>"
        "<style>body{margin:0;background:#0d1117;color:#8b949e;font:14px -apple-system,'Segoe UI',sans-serif}"
        "@media (prefers-color-scheme:light){body{background:#fff;color:#57606a}}"
        "main{max-width:880px;margin:32px auto;padding:0 16px}img{vertical-align:top}p{margin:16px 0}a{color:#58a6ff}</style>"
        f"<main>{body}</main>", encoding="utf-8")
    print("  preview/index.html (open it to screenshot)")


# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    global FONTS
    FONTS = Fonts()
    raw, is_sample = load_data()
    user = raw["user"]
    s = stats(calendar_days(user))
    CARDS.mkdir(exist_ok=True)
    for old in CARDS.glob("*.svg"):
        old.unlink()
    print(f"Rendering cards ({'STATIC final frame' if STATIC else 'animated'})")

    write("hero.svg", hero_card())
    write("heatmap.svg", heatmap_card(user, s, is_sample))
    write("activity.svg", activity_card(s, is_sample))
    for i, tile in enumerate(CONFIG["tiles"]):
        write(f'tile-{tile["id"]}.svg', tile_card(tile, i))
    write("title-dark.svg", title_card(True))
    write("title-light.svg", title_card(False))
    repos = []
    for i, r in enumerate(featured_repos(user)):
        fname = f"repo-{re.sub(r'[^a-z0-9]+', '-', r['name'].lower()).strip('-')}.svg"
        write(fname, repo_card(r, i, is_sample))
        repos.append((fname, r))

    update_readme(repos)
    if STATIC:
        write_preview()


if __name__ == "__main__":
    main()
