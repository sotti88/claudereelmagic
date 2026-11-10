# Style-Studio

Erweiterung von claudereelmagic (Copyright (c) 2026 Bianka Steinberger).

Auswahl-Oberfläche für den Untertitel-Look von Reels: fertige Looks, Schriften,
Farben, Hervorhebung, Größe, Kontur, Schatten, Schreibweise. Mit Live-Vorschau,
die **dieselbe Zeichenfunktion wie der Render** nutzt (`caption_style.draw_caption`).

## Öffnen

Cockpit starten, dann im Browser:

    http://127.0.0.1:8766/style

- Vorschau und Ausprobieren gehen immer.
- **Anwenden** speichert in das geöffnete **Reel-Projekt** (`sf_layout.json`, Eintrag
  `caption`). Position (`y_body`, `y_full`) und `lead_s` bleiben unverändert.
  Gespeichert wird mit Sperre, Revisionsprüfung und Backup wie im Cockpit.
- Danach das Cockpit neu laden (F5) und neu rendern.

## Eigene Schriften

`.ttf`- oder `.otf`-Dateien in `assets/fonts/user/` legen, Seite neu laden: sie
erscheinen automatisch als Kachel. Gespeichert wird nur der Dateiname (relativ zu
`assets/fonts/`), damit Projekte zwischen Mac, Windows und Cloud portabel bleiben.

**Lizenz beachten:** Nur Schriften verwenden, die du für den Einsatz in Videos
(und bei Weitergabe der Software) nutzen darfst, z. B. Google Fonts unter der OFL.
Mitgeliefert ist nur Inter (`assets/fonts/LICENSE-Inter.txt`).

## Technik

| Datei | Aufgabe |
|---|---|
| `scripts/style_studio.py` | Katalog, Prüfung der Eingaben, Vorschau, Anwenden |
| `scripts/cockpit/style-studio.html` | Oberfläche (eine Datei, keine externen Bibliotheken) |
| `scripts/cockpit_server.py` | Routen `/style`, `/api/style/catalog`, `/api/style/preview.png`, `/api/style/apply` |
| `scripts/caption_style.py` | Schriftname wird relativ zu `assets/fonts/` aufgelöst |

## Noch nicht enthalten

Animationen (Einblenden, Poppen, Gleiten), Hook-/Titel-Auswahl, Intro/Outro/Logo.
