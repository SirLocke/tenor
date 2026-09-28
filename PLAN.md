die weiße Typografie auch über hellen Bildstellen gestochen scharf und lesbar bleibt.
3. **Eigener Dark-Film-Filter (Moody Cinematic Look):**
   - Implementiere eine Bildverarbeitungskurve, die das typisch überschärfte/überbelichtete iPhone-Material bricht:
     - **Tiefen & Schwarzpunkt:** Sattes, leicht abgesenktes Schwarz mit mattem Auslauf (Matte Black Toe).
     - **Mitteltöne / Belichtung:** Leicht unterbelichtet (Gamma ~0.92–0.95), um eine düstere, atmosphärische Grundstimmung zu schaffen.
     - **Highlight-Roll-Off:** Spitzlichter dämpfen (Clip auf max. 92–95%), um digitales Ausbrennen am Himmel zu verhindern.
     - **Farbabstimmung:** Kühles, leicht entsättigtes Spektrum in den Schatten (Cine-Teal/Slate) kombiniert mit dezent gedämpften Haut- und Erdtönen.

---

### AUFGABEN-SCHRITTE (AUTOMATISCH AUSFÜHREN)

#### 1. Repository-Setup (`tenor`)
Erstelle und initialisiere das Git-Repository mit sauberer Projektstruktur:
- Ordnerstruktur:
  ```text
  tenor/
  ├── .gitignore
  ├── README.md
  ├── requirements.txt
  ├── config.yaml
  ├── src/
  │   ├── __init__.py
  │   ├── pipeline.py
  │   ├── metadata.py
  │   ├── styling.py
  │   └── watcher.py
  └── systemd/
      ├── tenor-watcher.service
      └── tenor-watcher.path

 * .gitignore: Ignoriere .venv/, Video-/Audioformate (*.mov, *.MOV, *.mp4, *.mp3), temporäre Render-Artefakte und Output-Ordner.
 * Initialisiere das lokale Git-Repo (git init -b main), setze einen Initial-Commit und dokumentiere Konfiguration und CLI-Nutzung in der README.md.
2. System-Abhängigkeiten & Python Virtual Environment
 * Prüfe und installiere Host-Pakete (ffmpeg, ffprobe, fonts-roboto).
 * Richte eine virtuelle Umgebung (python3 -m venv .venv) ein.
 * Installiere und fixiere:
   moviepy
librosa
opencv-python
pillow
geopy
numpy
pyyaml
watchdog

3. Core-Pipeline-Module (src/)
a) Metadaten & Geocoding (metadata.py):
 * Schnelles Parsen von creation_time bzw. com.apple.quicktime.creationdate via ffprobe JSON-Output.
 * ISO-6709 Geodaten aus QuickTime-Tags extrahieren.
 * Koordinaten über geopy (Nominatim, 5s Timeout, eigener User-Agent) zu Orten auflösen.
 * Chronologische Sortierung aller Rohdateien nach Aufnahmezeitpunkt sicherstellen.
b) Schnitt & Beat-Matching (pipeline.py):
 * Bei Übergabe einer MP3: Beat-Tracking via librosa.beat.beat_track, Zusammenfassung zu Downbeat-Intervallen (Ziel: 1,4 bis 2,0 Sekunden pro Cut).
 * Fallback: Fest konfigurierter Intervall-Takt (z.B. 1,8 Sekunden).
 * Zu lange Rohclips immer zentriert kürzen (subclip(mid - dur/2, mid + dur/2)).
c) Dark-Film & Typografie (styling.py):
 * Center-Crop: Automatisches Skalieren und Beschneiden auf exakt 1080x1920 Pixel (9:16).
 * Filter-Funktion: Numpy-basierte Pixel-Transformation für den Moody-Dark-Look (Kontrastkurve, Gamma 0.92, Highlight-Dämpfung, kühles Shadow-Color-Mapping).
 * Text-Overlay: Dynamische Pillow-Zeichenroutine ohne Hintergrundbox:
   * Rechtsbündige Ausrichtung mit x = 1080 - 180 = 900 als rechter Ankerpunkt.
   * Subtiler Drop-Shadow unter dem Text für hohen Kontrast.
d) Export & Cover:
 * Export stumm (audio=False), H.264 (libx264), yuv420p, 30 FPS.
 * Generiere automatisch ein 1080x1920 Cover-Thumbnail (.jpg) bei Sekunde 0.5.
4. Linux Watcher & Systemd-Automatisierung (watcher.py & systemd/)
 * Implementiere ein Watcher-Skript mit watchdog, das den Ingest-Ordner überwacht.
 * Debounce-Schutz: Warte mit der Verarbeitung, bis Dateien vollständig geschrieben sind (Größenprüfung über mindestens 5 Sekunden unverändert).
 * Erstelle die systemd-Dateien:
   * tenor-watcher.service: Startet das Python-Skript innerhalb des .venv.
   * Ausführung unter dem lokalen Benutzer; Neustartverhalten (Restart=on-failure, RestartSec=10).
 * Liefere die exakten systemctl-Befehle zur Aktivierung (daemon-reload, enable --now).
QUALITÄTSKRITERIEN
 * Keine Mockups oder Platzhalter: Vollständiger, getesteter Python-Code.
 * Isoliertes Error-Handling: Fehlerhafte Metadaten oder beschädigte Videoclips dürfen den Batch-Lauf nicht abbrechen.
 * Flexibilität: Sowohl interaktive CLI (python -m src.pipeline -i ... -o ... -t "Madeira 2026") als auch automatisierter Watcher-Betrieb.




