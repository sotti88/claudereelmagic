#!/usr/bin/env python3
"""Caption-Look (01.09.2026): eine Zeichenfunktion fuer rebuild_sf (Render)
und caption_preview.py (Vorschau) — nie zwei Implementierungen.

Stil kommt aus sf_layout.json -> "caption":
  font        Pfad zur TTF (Default: assets/fonts/Inter-Black.ttf)
  size        Default-Groesse (px im 1080x1920-Raum), Captions koennen c.size setzen
  uppercase   true/false
  text        [r,g,b] Textfarbe
  stroke      Konturbreite px (0 = keine)
  stroke_color[r,g,b]
  shadow      Schattenversatz px (0 = keiner)
  hot_style   "box" (Farbbox hinter dem aktiven Wort) | "color" (aktives Wort
              in hot-Farbe, keine Box) | "none"
  hot         [r,g,b] Hot-Farbe (Default Pink 212,67,106)
  box_radius  Eckenradius der Box
"""
import os
from PIL import Image, ImageDraw, ImageFont

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
_INTER = os.path.join(_ASSETS, "Inter-Black.ttf")

DEFAULT = {
    "font": _INTER if os.path.exists(_INTER) else "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "size": 62, "uppercase": True,
    "text": [255, 255, 255], "stroke": 6, "stroke_color": [0, 0, 0],
    "shadow": 0, "hot_style": "box", "hot": [212, 67, 106], "box_radius": 12,
}


def load_style(layout):
    st = dict(DEFAULT)
    st.update({k: v for k, v in (layout.get("caption") or {}).items() if v is not None})
    if not os.path.exists(st["font"]):
        st["font"] = DEFAULT["font"]
    return st


def draw_caption(W, H, words, hot_idx, size, style, x_mid, y_mid):
    """RGBA-Frame (W x H) mit EINER Caption-Seite; hot_idx = aktives Wort."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    font = ImageFont.truetype(style["font"], int(size))
    texts = [w.upper() for w in words] if style.get("uppercase", True) else list(words)
    widths = [dr.textlength(t, font=font) for t in texts]
    space = dr.textlength(" ", font=font)
    total = sum(widths) + space * (len(texts) - 1)
    if total > W - 60:                      # Notbremse: Seite zu breit
        size2 = max(28, int(size * (W - 60) / total))
        font = ImageFont.truetype(style["font"], size2)
        widths = [dr.textlength(t, font=font) for t in texts]
        space = dr.textlength(" ", font=font)
        total = sum(widths) + space * (len(texts) - 1)
    asc, desc = font.getmetrics()
    y_top = int(y_mid - (asc + desc) / 2)
    x = x_mid - total / 2
    tcol = tuple(style["text"]) + (255,)
    scol = tuple(style["stroke_color"]) + (255,)
    hot = tuple(style["hot"]) + (255,)
    sw = int(style.get("stroke", 0) or 0)
    sh = int(style.get("shadow", 0) or 0)
    for k, (txt, tw) in enumerate(zip(texts, widths)):
        is_hot = (k == hot_idx)
        if is_hot and style.get("hot_style") == "box":
            dr.rounded_rectangle([x - 16, y_top - 4, x + tw + 16, y_top + asc + desc + 2],
                                 radius=int(style.get("box_radius", 12)), fill=hot)
        fill = hot if (is_hot and style.get("hot_style") == "color") else tcol
        if sh:
            dr.text((x + sh, y_top + sh), txt, font=font, fill=(0, 0, 0, 150),
                    stroke_width=sw, stroke_fill=(0, 0, 0, 150))
        dr.text((x, y_top), txt, font=font, fill=fill, stroke_width=sw, stroke_fill=scol)
        x += tw + space
    return img
