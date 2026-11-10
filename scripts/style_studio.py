"""Style-Studio: Katalog (Schriften, Farben, Looks), Vorschau und Anwenden.

Erweiterung von claudereelmagic (Copyright (c) 2026 Bianka Steinberger).
Baut auf caption_style.py der Basis (claude-video-cutter, MIT) auf: die
Vorschau nutzt EXAKT dieselbe Zeichenfunktion wie der Render
(caption_style.draw_caption) - was du siehst, ist was gerendert wird.

Schriften: alle .ttf/.otf in assets/fonts/ und assets/fonts/user/ erscheinen
automatisch als Kachel. Eigene Schriften einfach in assets/fonts/user/ legen.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image, ImageDraw

import caption_style

FONT_ROOT = (Path(__file__).resolve().parent.parent / "assets" / "fonts").resolve()
FONT_EXT = {".ttf", ".otf"}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

HIGHLIGHTS = [
    {"id": "box", "label": "Farbbox", "hint": "Aktives Wort in einer Box"},
    {"id": "color", "label": "Farbe", "hint": "Aktives Wort in Farbe"},
    {"id": "none", "label": "Keine", "hint": "Kein Hervorheben"},
]
SIZES = [("Klein", 52), ("Mittel", 62), ("Groß", 72), ("Riesig", 86)]
STROKES = [("Keine", 0), ("Dünn", 3), ("Mittel", 6), ("Dick", 10)]
SHADOWS = [("Aus", 0), ("Leicht", 3), ("Stark", 7)]
RADII = [("Eckig", 0), ("Rund", 12), ("Pille", 30)]

PALETTES = [
    {"id": "klassik", "label": "Klassik Pink", "text": "#FFFFFF", "hot": "#D4436A", "stroke": "#000000"},
    {"id": "gold", "label": "Gold", "text": "#F5E6C8", "hot": "#D4AF37", "stroke": "#000000"},
    {"id": "neon", "label": "Neon Cyan", "text": "#FFFFFF", "hot": "#22E3FF", "stroke": "#0B1020"},
    {"id": "sonne", "label": "Sonne", "text": "#FFD60A", "hot": "#FFFFFF", "stroke": "#000000"},
    {"id": "violett", "label": "Violett", "text": "#FFFFFF", "hot": "#7C3AED", "stroke": "#000000"},
    {"id": "mint", "label": "Mint", "text": "#FFFFFF", "hot": "#2DD4A7", "stroke": "#0B3B32"},
    {"id": "koralle", "label": "Koralle", "text": "#FFFFFF", "hot": "#FF6B57", "stroke": "#3A0E08"},
    {"id": "invers", "label": "Dunkel auf Hell", "text": "#111111", "hot": "#E11D48", "stroke": "#FFFFFF"},
]


def _look(id_, label, desc, font, size, upper, pal, stroke, shadow, hot_style, radius):
    p = next(x for x in PALETTES if x["id"] == pal)
    return {"id": id_, "label": label, "desc": desc, "style": {
        "font": font, "size": size, "uppercase": upper, "text": p["text"], "hot": p["hot"],
        "stroke": stroke, "stroke_color": p["stroke"], "shadow": shadow,
        "hot_style": hot_style, "box_radius": radius}}


LOOKS = [
    _look("mutig", "Groß & mutig", "Dicke Großbuchstaben, pinke Box", "Inter-Black.ttf", 70, True, "klassik", 6, 0, "box", 12),
    _look("edel", "Ruhig & edel", "Dezent, goldenes Wort, ohne Kontur", "Inter-SemiBold.ttf", 54, False, "gold", 0, 4, "color", 0),
    _look("pop", "Pop-Box", "Violette Box, kein Rand", "Inter-ExtraBold.ttf", 64, True, "violett", 0, 0, "box", 30),
    _look("neon", "Neon", "Cyan leuchtet, dunkler Rand", "Inter-Black.ttf", 66, True, "neon", 4, 6, "color", 0),
    _look("clean", "Klar & clean", "Normale Schreibweise, ohne Hervorhebung", "Inter-Bold.ttf", 58, False, "klassik", 5, 0, "none", 0),
    _look("premium", "Gold-Premium", "Elegantes Gold mit Schatten", "Inter-ExtraBold.ttf", 60, True, "gold", 0, 5, "color", 0),
    _look("sonne", "Sonnenschein", "Gelber Text, schwarzer Rand", "Inter-Black.ttf", 68, True, "sonne", 7, 0, "color", 0),
]

SAMPLE_WORDS = ["Das", "ist", "dein", "Look"]


# --- Schriften ---------------------------------------------------------------
def _label(stem: str) -> str:
    s = re.sub(r"[_\-]+", " ", stem)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def list_fonts() -> list[dict]:
    """Alle Schriften in assets/fonts (+ user/). Inter-Black zuerst (Standard)."""
    found = []
    if FONT_ROOT.is_dir():
        for p in sorted(FONT_ROOT.rglob("*")):
            if p.suffix.lower() in FONT_EXT and p.is_file():
                rel = p.relative_to(FONT_ROOT).as_posix()
                found.append({"id": rel, "label": _label(p.stem), "user": rel.startswith("user/")})
    found.sort(key=lambda f: (f["id"] != "Inter-Black.ttf", f["user"], f["label"].lower()))
    return found


def resolve_font(font_id: str) -> Path | None:
    """Schrift-ID -> Pfad, nur INNERHALB von assets/fonts (kein Pfad-Trick)."""
    if not isinstance(font_id, str) or not font_id:
        return None
    path = Path(font_id)
    candidate = path if path.is_absolute() else FONT_ROOT / font_id
    try:
        candidate = candidate.resolve()
        candidate.relative_to(FONT_ROOT)
    except (OSError, ValueError):
        return None
    if candidate.suffix.lower() in FONT_EXT and candidate.is_file():
        return candidate
    return None


# --- Normalisieren -----------------------------------------------------------
def hex_to_rgb(value: str) -> list[int]:
    if not isinstance(value, str) or not HEX_RE.match(value):
        raise ValueError("Ungültige Farbe: %r" % (value,))
    return [int(value[i:i + 2], 16) for i in (1, 3, 5)]


def rgb_to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, c))) for c in rgb)


def _num(value, lo, hi, name):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("%s muss eine Zahl sein" % name)
    return max(lo, min(hi, n))


def normalize(raw: dict) -> dict:
    """UI-Werte (Hex-Farben, Schrift-ID) -> Werte für sf_layout.json 'caption'.

    Nur bekannte Schlüssel, alle Zahlen begrenzt. Wirft ValueError bei Unsinn.
    """
    raw = raw or {}
    font_id = raw.get("font") or "Inter-Black.ttf"
    font = resolve_font(font_id)
    if font is None:
        raise ValueError("Schrift nicht gefunden: %r" % (font_id,))
    hot_style = raw.get("hot_style", "box")
    if hot_style not in {h["id"] for h in HIGHLIGHTS}:
        raise ValueError("Unbekannte Hervorhebung: %r" % (hot_style,))
    upper = raw.get("uppercase", True)
    if isinstance(upper, str):
        upper = upper.lower() in ("1", "true", "ja", "yes")
    return {
        "font": font.relative_to(FONT_ROOT).as_posix(),
        "size": _num(raw.get("size", 62), 28, 140, "size"),
        "uppercase": bool(upper),
        "text": hex_to_rgb(raw.get("text", "#FFFFFF")),
        "hot": hex_to_rgb(raw.get("hot", "#D4436A")),
        "stroke": _num(raw.get("stroke", 6), 0, 16, "stroke"),
        "stroke_color": hex_to_rgb(raw.get("stroke_color", "#000000")),
        "shadow": _num(raw.get("shadow", 0), 0, 12, "shadow"),
        "hot_style": hot_style,
        "box_radius": _num(raw.get("box_radius", 12), 0, 40, "box_radius"),
    }


def to_ui(style: dict) -> dict:
    """Gespeicherter Stil -> UI-Werte (Hex)."""
    out = dict(style)
    for key in ("text", "hot", "stroke_color"):
        if isinstance(out.get(key), (list, tuple)) and len(out[key]) == 3:
            out[key] = rgb_to_hex(out[key])
    font = resolve_font(out.get("font", ""))
    out["font"] = font.relative_to(FONT_ROOT).as_posix() if font else "Inter-Black.ttf"
    return out


# --- Katalog -----------------------------------------------------------------
def catalog(current_layout: dict | None = None) -> dict:
    cur = None
    if current_layout is not None:
        cur = to_ui(caption_style.load_style(current_layout))
    return {
        "fonts": list_fonts(),
        "looks": LOOKS,
        "palettes": PALETTES,
        "highlights": HIGHLIGHTS,
        "sizes": [{"label": a, "value": b} for a, b in SIZES],
        "strokes": [{"label": a, "value": b} for a, b in STROKES],
        "shadows": [{"label": a, "value": b} for a, b in SHADOWS],
        "radii": [{"label": a, "value": b} for a, b in RADII],
        "sample_words": SAMPLE_WORDS,
        "current": cur,
    }


# --- Vorschau ----------------------------------------------------------------
def render_preview(raw_style: dict, words=None, hot_idx: int = 1, width: int = 720) -> bytes:
    """PNG-Vorschau mit derselben Zeichenfunktion wie der Render."""
    style = normalize(raw_style)
    st = dict(caption_style.DEFAULT)
    st.update(style)
    st["font"] = str(resolve_font(style["font"]))
    words = [w[:20] for w in (words or SAMPLE_WORDS) if str(w).strip()][:4] or SAMPLE_WORDS
    hot_idx = hot_idx if -1 <= hot_idx < len(words) else 0  # -1 = kein Wort hervorgehoben
    width = max(200, min(900, int(width)))
    W, H = 1080, 300
    layer = caption_style.draw_caption(W, H, words, hot_idx, st["size"], st, W // 2, H // 2)
    bg = Image.new("RGBA", (W, H))
    px = ImageDraw.Draw(bg)
    for y in range(H):  # ruhiger dunkler Verlauf, damit helle UND dunkle Texte lesbar bleiben
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=(int(40 + 30 * t), int(44 + 22 * t), int(66 + 40 * t), 255))
    bg.alpha_composite(layer)
    out = bg.convert("RGB").resize((width, int(H * width / W)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --- Anwenden ----------------------------------------------------------------
def apply_to_project(workdir: str, raw_style: dict) -> dict:
    """Stil ins Projekt schreiben (sf_layout.json 'caption'), alles andere bleibt.

    Nutzt sf_project.save_settings: gleiche Sperre, Revisions-Prüfung und
    Backups wie das Cockpit selbst. Position (y_body, y_full) und lead_s
    bleiben unverändert.
    """
    import sf_project
    style = normalize(raw_style)
    current = sf_project.load_settings(workdir)
    layout = dict(current["layout"])
    caption = dict(layout.get("caption") or {})
    caption.update(style)
    layout["caption"] = caption
    return sf_project.save_settings(workdir, {"layout": layout, "revision": current["revision"]})
