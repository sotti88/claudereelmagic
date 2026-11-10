# Setup-Prompt

Diesen Prompt komplett kopieren und in Claude Code pasten - Claude installiert dann alles mit dir zusammen (die INSTALL.md im Repo ist an Claude adressiert):

---

Ich will mir das Cutting-Cockpit aus dem Resource Pack einrichten: https://github.com/sebaskauf/claude-video-cutter

1. Clone das Repo: `git clone https://github.com/sebaskauf/claude-video-cutter.git ~/Documents/Projects/claude-video-cutter` (falls der Ordner schon existiert: dort `git pull` statt clone). Geh in den Ordner.
2. Lies die INSTALL.md im Repo und führ sie komplett mit mir durch. Sie ist an dich adressiert: Voraussetzungen prüfen (Python 3.12, ffmpeg), die Python-Umgebung bauen, Agent + Skills mit dem richtigen Pfad installieren. Was fehlt, installierst du mit mir zusammen - sag mir vorher, was und warum.
3. Für die Transkription brauche ich einen AssemblyAI-Schlüssel: sag mir, wo ich ihn hole (assemblyai.com, kostenloses Konto), und leg ihn als AAI_KEY in die .env (Vorlage .env.example liegt im Repo). Ein Video kostet Cent-Beträge.
4. Falls ich das Agentic OS nutze: richte zusätzlich den CUTTER-Tab nach INSTALL-AGENTIC-OS.md ein. Falls nicht: alles läuft genauso im Browser.
5. Danach ein Testlauf mit einer kurzen Datei (1 bis 2 Minuten): [PFAD ZU EINEM KURZEN TESTVIDEO]. Starte dafür Claude Code einmal neu, damit Agent und Skills geladen sind, und lass dann den video-cutter-Agenten den kompletten Durchlauf machen.
6. Wenn am Ende das Cockpit im Browser aufgeht, sind wir fertig.

Wenn irgendwas scheitert, zeig mir die genaue Fehlermeldung, statt es als erledigt zu melden.

---

Danach schneidet ein Prompt dein Video (steht auch im Resource Pack unter dem Video):

---

Schneide dieses Video: [PFAD ZUR ROHDATEI]

Mach den kompletten Durchlauf: transkribieren (Wort für Wort, mit allen Wiederholungen), Schnitt-Entscheidungen treffen (Versprecher, doppelte Anläufe, falsche Starts raus - bei mehreren Takes gewinnt immer der letzte), sample-genau schneiden an den leisesten Stellen, jede einzelne Schnittstelle selbst prüfen. Öffne mir danach das Cockpit für den Review. Melde kein "fertig", ohne dass du den Schnitt am gerenderten Ergebnis geprüft hast.

---
