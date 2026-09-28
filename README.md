# tenor

Automatische Video-Pipeline für den Heimserver: überwacht einen Ingest-Ordner,
ordnet iPhone-Clips chronologisch, schneidet sie auf den Beat, versieht sie mit
einem Dark-Film-Grading und exportiert stumme **1080x1920 (9:16)** Reels/Shorts
inklusive Cover-Thumbnail.

```
Rohclips (MOV/MP4)                     Reels + Cover
/srv/nas/Nils/video-cut/ingest   ──►  /srv/nas/Nils/video-cut/output
        │  watchdog + Debounce                    │
        │  ffprobe (Zeit/GPS)                     └─ 1080x1920 H.264, yuv420p, 30 fps, stumm
        │  librosa Beat-Grid
        └─ center-crop 9:16 → Dark-Film-LUT → rechtsbündige Typografie
```

## Ordnerstruktur

Die Clips werden nach **Tripname und Aufnahmetag** sortiert. Der Tripname ist
der Ordnername unterhalb von `ingest_dir`:

```
/srv/nas/Nils/video-cut/
├── ingest/                     ← hier landen die Rohclips
│   └── Madeira 2026/           ← Ordnername = Tripname
│       ├── IMG_1234.MOV
│       └── IMG_1235.MOV
├── music/                      ← MP3 fürs Beat-Matching
└── output/                     ← Ergebnis
    └── Madeira-2026/           ← {trip}, slugifiziert
        ├── 2026-09-11/         ← {day}, Aufnahmetag aus den Metadaten
        │   ├── 2026-09-28_Madeira-2026_183045.mp4
        │   └── 2026-09-28_Madeira-2026_183045.jpg
        └── 2026-09-12/         ← jeder Tag wird ein eigenes Reel
            └── ...
```

Clips ohne Aufnahmetag landen gemeinsam in `_ohne-datum`. Fehlen in den
Metadaten Zeit und GPS, greift als Rückfall die Dateizeit (mtime).

Beide Ebenen sind über `naming.folder_template` steuerbar:

| Platzhalter | Bedeutung              | Beispiel       |
| ------------ | ---------------------- | -------------- |
| `{trip}`     | Tripname, slugifiziert | `Madeira-2026` |
| `{day}`      | Aufnahmetag            | `2026-09-11`   |
| `{date}`     | Exportdatum            | `2026-09-28`   |
| `{time}`     | Exportzeit             | `183045`       |
| `{slug}`     | Titel, slugifiziert    | `madeira-2026` |
| `{title}`    | Titel, slugifiziert    | `madeira-2026` |

Leerer Wert (`folder_template: ""`) schreibt direkt nach `output_dir` ohne
Unterordner.

## Features

- **Chronologische Sortierung** über QuickTime-Tags (`creationdate`,
  `contentcreationdate`) – nicht über mtime, damit Kopiervorgänge nichts
  verfälschen. ISO-6709-Koordinaten aus den iPhone-Tags werden gelesen.
- **Reverse-Geocoding** über Nominatim (geopy, 5 s Timeout, eigener
  User-Agent) mit **persistenter Cache-Datei** – pro Koordinate nur eine Anfrage.
- **Beat-Matching**: `librosa.beat.beat_track` gruppiert Beats zu Downbeats, so
  dass ein Cut zwischen 1,4 s und 2,0 s liegt. Ohne MP3 greift ein fester
  Takt (1,8 s).
- **Dark-Film-Grading** in zwei Stufen: 256er-Tonwertkurve (Gamma, S-Kurve,
  Matte Black Toe, Highlight-Roll-Off) als LUT via OpenCV, danach eine
  Farbstufe (Entsättigung, Cine-Teal in den Schatten, kühle Highlights).
- **Typografie ohne Box**: rechtsbündig bei `x = 900`, Textgruppe in der
  Safe Zone `y = 1440…1540`, Versalien mit Letter-Spacing, weicher Drop-Shadow
  (Offset 2/2, 70 % Alpha) für Lesbarkeit über hellen Bildstellen.
- **Fehlerisolation**: kaputte Metadaten, nicht lesbare Dateien und
  fehlschlagende Cuts werden geloggt und übersprungen – der Batch-Lauf bricht
  nie ab.
- **Watcher mit Debounce**: eine Datei gilt erst als verarbeitet, wenn ihre
  Größe 5 s unverändert bleibt. Danach wird noch ein Batch-Fenster gesammelt,
  damit ein iPhone-Burst in *einem* Reel landet.
- **Ein Reel pro Tag**: Material aus mehreren Aufnahmetagen wird automatisch
  getrennt, damit im Ordner `2026-09-12/` auch wirklich nur der 12.09. liegt.
- **Tripname aus dem Ingest-Ordner**: `ingest/Madeira 2026/` ergibt
  `output/Madeira-2026/` – ohne einen Titelparameter überall.
- **State-Datei** (`.tenor-state.json`) verhindert Doppelverarbeitung nach
  Neustarts. Verwaiste `*.part.mp4` aus abgebrochenen Läufen werden beim Start
  entfernt.

## Installation

### 1. Systemabhängigkeiten

```bash
sudo apt update
sudo apt install -y ffmpeg fonts-roboto     # ffprobe ist in ffmpeg enthalten
```

`fonts-roboto` ist optional: fehlt der Font, lädt `tenor` ihn einmalig nach
`assets/fonts/` und nutzt sonst einen Systemfont (Lato/Noto/DejaVu).

### 2. Repository & Virtualenv

```bash
git clone https://github.com/SirLocke/tenor.git ~/video-cut
cd ~/video-cut
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 3. Konfiguration

`config.yaml` anpassen – mindestens `ingest_dir`, `output_dir`, `music_dir`:

```yaml
ingest_dir: /srv/nas/Nils/video-cut/ingest
output_dir: /srv/nas/Nils/video-cut/output
music_dir: /srv/nas/Nils/video-cut/music
```

Werte lassen sich auch per Umgebungsvariable überschreiben – der Name ist der
Pfad in Großbuchstaben, mit `_` statt `.`:

```bash
TENOR_INGEST_DIR=/mnt/usb/clips TENOR_VIDEO_FPS=24 python -m src.pipeline --dry-run
TENOR_CUT_FALLBACK_INTERVAL=2.5 python -m src.watcher      # Fallback-Takt
TENOR_CONFIG=./config.yaml python -m src.watcher           # andere Config-Datei
```

Priorität (steigend): Defaults → `config.yaml` → CLI-Flags → `TENOR_*`.

Der Grading-Look steckt komplett in `style:`:

| Key | Default | Wirkung |
| --- | --- | --- |
| `gamma` | `0.93` | Belichtung: **unter** 1.0 unterbelichtet (düster), über 1.0 hellt auf |
| `contrast` | `0.22` | Stärke der S-Kurve |
| `black_lift` / `black_toe` | `0.035` / `2.2` | Matte Black Toe: matter, leicht angehobener Schwarzpunkt |
| `highlight_ceiling` | `0.94` | Highlight-Roll-Off, verhindert ausgebrannte Himmel |
| `saturation` / `shadow_desaturate` | `0.90` / `0.18` | leicht entsättigt, Schatten stärker |
| `teal_red/green/blue` | `-0.006 / 0.012 / 0.026` | Cine-Teal in den Schatten |
| `highlight_cool` | `0.010` | Lichter kühlen |
| `skin_damp` | `0.008` | Haut-/Erdtöne in den Mitteltönen dämpfen |

`style.text` steuert die Typografie (`anchor_x: 900`, `y_title: 1445`,
`safe_bottom: 1540`, Schriftgrößen, Tracking, Shadow).

## CLI-Nutzung

```bash
# Kompletter Ordner, Beat-Matching über die neueste MP3 im music_dir.
# Ohne -t wird der Ordnername unterhalb von ingest_dir zum Tripnamen.
.venv/bin/python -m src.pipeline -i /srv/nas/Nils/video-cut/ingest

# Tripname und Ausgabeordner explizit
.venv/bin/python -m src.pipeline -i /srv/nas/Nils/video-cut/ingest -t "Madeira 2026"

# Einzelne Datei, explizite Musik
.venv/bin/python -m src.pipeline -i clip.MOV -o out/ -t "Madeira" -m song.mp3

# Nur den Schnittplan ansehen, kein Export (legt nichts an)
.venv/bin/python -m src.pipeline -i /srv/nas/Nils/video-cut/ingest --dry-run

# Metadaten inspizieren (Debug)
.venv/bin/python -m src.metadata -i /srv/nas/Nils/video-cut/ingest

# Watcher manuell starten
.venv/bin/python -m src.watcher --config config.yaml
```

Wichtige Flags der Pipeline: `--no-cover`, `--no-geo` (ohne Netzwerk),
`--limit N`, `--json`, `--delete-after` (Rohclits nach Export löschen),
`--dry-run`.

### Ausgabe

Pro Aufnahmetag ein Reel, inklusive Cover:

```
output/Madeira-2026/2026-09-11/
├── 2026-09-28_Madeira-2026_134838.mp4   H.264, yuv420p, 30 fps, 1080x1920, stumm
└── 2026-09-28_Madeira-2026_134838.jpg   Cover-Frame bei Sekunde 0.5
```

`--json` listet alle erzeugten Reels:

```json
{
  "outputs": [".../2026-09-11/….mp4", ".../2026-09-12/….mp4"],
  "covers":  [".../2026-09-11/….jpg", ".../2026-09-12/….jpg"],
  "cuts": 5, "duration": 7.0, "trip": "Madeira 2026"
}
```

Geschrieben wird zunächst nach `*.part.mp4` und erst nach erfolgreichem
Abschluss atomar umbenannt – ein abgebrochener Lauf hinterlässt keine
halben Dateien.

## Automatisierung via systemd

```bash
# Units installieren
mkdir -p ~/.config/systemd/user
cp ~/video-cut/systemd/tenor-watcher.service ~/.config/systemd/user/
cp ~/video-cut/systemd/tenor-watcher.path   ~/.config/systemd/user/

# Reload + aktivieren
systemctl --user daemon-reload
systemctl --user enable --now tenor-watcher.service

# Status / Logs
systemctl --user status tenor-watcher.service
journalctl --user -u tenor-watcher.service -f
```

Die Units laufen als **Benutzer-Dienst** (`--user`, kein `sudo` beim Rendern
nötig). Für User-Dienste ggf. einmalig aktivieren:

```bash
sudo loginctl enable-linger nils     # sonst kein Start nach dem Abmelden
```

Alternativ ereignisgesteuert statt dauerhaft überwachen:

```bash
systemctl --user disable --now tenor-watcher.service
systemctl --user enable --now tenor-watcher.path
```

Hinweis: Der `.path`-Unit startet den Service nur bei Änderungen direkt in
`ingest_dir`. Für Clips in Trip-Unterordnern den **Service** dauerhaft
aktivieren (Variante oben) – er überwacht rekursiv.

Alle Pfade in der Unit stehen als `%h/…` (Home des Users). Wenn das Repo woanders
liegt, `WorkingDirectory=` und `ExecStart=` anpassen.

## Projektstruktur

```
tenor/
├── .gitignore          # ignoriert .venv, Roh-/Audioformate, Render-Artefakte, Output
├── README.md
├── requirements.txt    # gepinnte Abhängigkeiten
├── config.yaml         # Pfade, Grading, Typografie, Watcher
├── src/
│   ├── __init__.py     # Konfigurations-Infrastruktur (Defaults + Deep-Merge)
│   ├── pipeline.py     # Beat-Matching, Schnittplan, Rendern, Export, Cover, CLI
│   ├── metadata.py     # ffprobe, ISO-6709, Reverse-Geocoding, Sortierung, CLI
│   ├── styling.py      # LUT-Grading, Center-Crop, Font-Auflösung, Text-Overlay
│   └── watcher.py      # watchdog-Ingest mit Debounce + Batch-Fenster
└── systemd/
    ├── tenor-watcher.service
    └── tenor-watcher.path
```

### Datenfluss im Detail

1. **`metadata.py`** – `ffprobe -print_format json` liefert Format- und
   Stream-Tags. Der Aufnahmezeitpunkt kommt aus den QuickTime-Tags
   (`com.apple.quicktime.creationdate`), notfalls aus dem Datum im
   Location-Tag, notfalls mtime. `parse_coordinates()` versteht
   `+53.4702-006.1153+023.5/`, `+5328-00607/` und `53°28'12"N 6°06'55"W`.
   `Geocoder` löst Koordinaten über Nominatim auf und cacht sie in
   `.cache/geocoding.json` (inklusive Negativ-Caching bei Netzwerkfehlern).
2. **`pipeline.py`** – `trip_from_path()` liest den Tripnamen aus dem
   Ingest-Unterordner, `build_segments()` legt die Cuts chronologisch fest und
   kürzt zu langes Material zentriert (`mid - dur/2 … mid + dur/2`). Cuts,
   die unter `target_min` fallen, werden verworfen. Anschließend gruppiert
   `run()` das Material nach Aufnahmetag und ruft für jede Gruppe
   `export_reel()` auf.
3. **`styling.py`** – `center_crop_resize()` beschneidet auf exakt 9:16,
   `make_grader()` legt die LUT und die Farbstufe auf jeden Frame,
   `build_text_overlay()` rendert Titel/Unterzeile rechtsbündig mit Shadow.
4. **Export** – `write_videofile()` mit `libx264`, `yuv420p`, 30 fps,
   `audio=False`; danach das Cover via `ffmpeg -ss 0.5`.

## Fehlerbehandlung

| Situation | Verhalten |
| --- | --- |
| `ffprobe` schlägt fehl / Datei kaputt | Warnung, Datei wird über mtime einsortiert bzw. übersprungen |
| Clip scheitert beim Rendern | Cut wird verworfen, restliche Cuts rendern weiter |
| Kein Beat-Grid (Musik zu kurz/leise) | Fallback-Intervall aus `cut.fallback_interval` |
| Nominatim nicht erreichbar | Negativ gecacht, Lauf läuft ohne Ortsangabe weiter |
| Kein Font ladbar | Pillow-Defaultfont als letzte Instanz |
| Export abbruch | `*.part.mp4` wird gelöscht, kein unfertiges Ergebnis |
| `ResizeObserver`-Ereignisse während des Schreibens | Debounce wartet auf stabile Dateigröße |

## Exit-Codes

`0` = Reel geschrieben (oder `--dry-run` erfolgreich), `1` = kein Export
möglich, `2` = Konfigurations-/Aufruffehler.
