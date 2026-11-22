# tenor

[![CI](https://github.com/SirLocke/tenor/actions/workflows/ci.yml/badge.svg)](https://github.com/SirLocke/tenor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Automatische Video-Pipeline für den Heimserver: überwacht einen Ingest-Ordner,
ordnet iPhone-Clips chronologisch, schneidet sie auf den Beat, versieht sie mit
einem Dark-Film-Grading und exportiert **1080x1920 (9:16)** Reels/Shorts
inklusive Cover-Thumbnail – stumm, mit bewusst gewählter Musik oder mit dem
Originalton der Clips.

```
Rohclips (MOV/MP4)                     Reels + Cover
~/video-cut/ingest   ──►  ~/video-cut/output
        │  watchdog + Debounce                    │
        │  ffprobe (Zeit/GPS)                     └─ 1080x1920 H.264, yuv420p, 30 fps
        │  librosa Beat-Grid                         (Ton nur mit -m/--audio/AAC)
        └─ center-crop 9:16 → Dark-Film-LUT → rechtsbündige Typografie
```

## Ordnerstruktur

Die Clips werden nach **Tripname und Aufnahmetag** sortiert. Der Tripname ist
der Ordnername unterhalb von `ingest_dir`:

```
~/video-cut/
├── ingest/                     ← hier landen die Rohclips
│   └── Madeira 2026/           ← Ordnername = Tripname
│       ├── IMG_1234.MOV
│       └── IMG_1235.MOV
├── music/                      ← MP3s: Beat-Matching, gewählte Musik landet im Reel
├── .cache/                     ← zwischengespeicherte Analysen
│   ├── geocoding.json
│   └── quality/                ← Graustufen-Strips je Rohclip (Key aus Pfad/mtime)
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
- **Beat-Matching** in drei Stufen, automatisch gewählt:
  1. **Spotify Audio-Analysis** – echte Beat-Timestamps eines Tracks
     (`spotify.track_uri`, braucht `SPOTIFY_CLIENT_ID`/`SECRET`).
     **Achtung:** seit dem 27.11.2024 gibt Spotify diesen Endpoint nur noch an
     Apps heraus, die damals schon eine *Extended Quota* hatten. Jede später
     registrierte App bekommt dauerhaft HTTP 403 – dann springt `track_file`
     bzw. Stufe 2 ein, statt stumm auf 120 BPM zu fallen (s.
     [Spotify-Beat-Matching](#spotify-beat-matching-optional)).
  2. **Lokale Audiodatei** – `spotify.track_file` (die Audiospur des gewählten
     Tracks) oder die *neueste* Datei im `music_dir`. `librosa.beat.beat_track`
     gruppiert die Beats zu Downbeats, so dass ein Cut zwischen `target_min`
     und `target_max` liegt. Es zählen `.mp3`, `.m4a`, `.aac`, `.wav`, `.flac`,
     `.ogg`, `.opus`.
  3. **BPM-Fallback** – `cut.fallback_bpm` (Default 120) erzeugt ein
     künstliches Beat-Grid: Beat = 60/BPM, gruppiert auf ~`cut.target`.
     120 BPM ⇒ 0,5 s Beats ⇒ 3er-Gruppe ⇒ 1,5 s Cuts.

  Musik steuert **immer** den Schnitttakt. Ob sie auch *im Reel* zu hören ist,
  entscheidet die Wahl: nur eine **bewusst gewählte** Musik wird untergemischt
  (`-m <datei>` – auch bei externer Auswahl – oder `-m auto`, das auf `music.file`
  zeigt). Ein Lauf ohne `-m` bleibt stumm; der Automatismus
  (`spotify.track_file`, neueste Datei aus `music_dir`) liefert weiterhin nur den
  Takt. Genau dieser Unterschied war die Ursache für stumme Reels: eine explizite
  Auswahl kam als `-m` an und steuerte den Schnitt – der Export schrieb die
  Spur aber nicht ins Video.
- **Musik im Reel** – liegt bewusst gewählte Musik vor, wird sie unter das
  fertige Bild gelegt: auf die Reellänge gebracht (kürzere Tracks loopen,
  längere werden geschnitten), auf Kanalzahl und Abtastrate der übrigen Spur
  gebracht, mit `music.gain` pegelt und über `music.fade_out` ausgeblendet. Ist
  zusätzlich `video.audio: true` aktiv, läuft der Originalton **gedämpft
  darunter** (`video.original_gain`, Default 0,25) – hörbar, aber nicht über der
  Musik. Fehlt die Datei oder ist sie unlesbar, bleibt der Export stumm und
  läuft weiter; ein kaputter Track kostet nie das ganze Video.
- **Optionaler Originalton** – mit `video.audio: true` bzw. `--audio` wird der
  Ton der Rohclips mit exportiert (AAC, `video.audio_bitrate`, default 192 k).
  Der Ton wird als eigener Ausschnitt aus der Quelle geschnitten, nicht aus dem
  gegradeten Bild-Clip geerbt, und bleibt damit Frame-genau am Bild. Clips ohne
  Tonspur werden für ihre Dauer stumm – das Reel verschiebt sich nicht.
- **Bildqualitäts-Gate** – vor dem Rendern wird pro Rohclip ein schmaler
  Graustufen-Strip gezogen (ein ffmpeg-Durchlauf, Ergebnis unter
  `quality.cache_dir` zwischengespeichert) und jedes geplante Cut-Fenster
  damit bewertet:
  - *lokal*: Schärfe (Laplacian-Varianz), Belichtung, Schwarz-/Weißanteil –
    gemessen **vor** dem Grading, weil das Dark-Film-Look die Luma absenkt
  - *global*: 16×16-Bildsignatur gegen die zuletzt gezeigten Cuts, damit
    iPhone-Bursts und zwei Schnitte aus demselben statischen Shot nicht
    zweimal im Reel stehen

  Die Fenster werden über den ganzen Clip verteilt (vorher klebte der
  Treppenlauf am Rest und ließ bei langen Clips über die Hälfte ungenutzt)
  und durchgefallene Fenster werden verschoben, bis sie passen. Eine echte
  Wiederholung wird **nicht** wegschiebbar gerettet – 0,3 s weiter steht bei
  einem Burst immer noch derselbe Moment. Unsauberes Bildmaterial wird
  dagegen behalten: ein weiches Bild ist besser als eine Lücke im Reel.
  Ist eine Datei nicht auswertbar (kaputt, Timeout), wird nicht bewertet,
  sondern alles unverändert übernommen.
- **Dark-Film-Grading** in zwei Stufen: 256er-Tonwertkurve (Gamma, S-Kurve,
  Matte Black Toe, Highlight-Roll-Off) als LUT via OpenCV, danach eine
  Farbstufe (Entsättigung, Cine-Teal in den Schatten, kühle Highlights).
- **Typografie ohne Box**: rechtsbündig bei `x = 900`, Textgruppe in der
  Safe Zone `y = 1440…1540`, Versalien mit Letter-Spacing, weicher Drop-Shadow
  (Offset 2/2, 70 % Alpha) für Lesbarkeit über hellen Bildstellen.
  Standardmäßig nur auf den **ersten 2 Cuts** (`text_on_first_cuts: 2`) – der
  Hook trägt die Beschriftung, der Rest bleibt clean. `0` = auf allen Cuts.
- **Fehlerisolation**: kaputte Metadaten, nicht lesbare Dateien und
  fehlschlagende Cuts werden geloggt und übersprungen – der Batch-Lauf bricht
  nie ab. Ein fehlgeschlagener Cut wird übersprungen, **die übrigen bleiben
  im Reel** (kein Loch am Anfang).
- **Watcher mit Debounce**: eine Datei gilt erst als verarbeitet, wenn ihre
  Größe 5 s unverändert bleibt. Danach wird noch ein Batch-Fenster gesammelt,
  damit ein iPhone-Burst in *einem* Reel landet. Harte Obergrenze 6× Debounce,
  damit eine endlos wachsende Datei keinen Thread hängen lässt.
- **Fehlgeschlagene Batches werden wiederholt** (`watcher.retry_limit: 3`,
  `retry_backoff: 300`) – ein ffmpeg-OOM oder NAS-Aussetzer kostet das
  Material nicht mehr, es liegt nur 5 min und wird erneut versucht.
- **Ein Reel pro Tag**: Material aus mehreren Aufnahmetagen wird automatisch
  getrennt, damit im Ordner `2026-09-12/` auch wirklich nur der 12.09. liegt.
  Bewusst zusammenführen: `--single-reel` (Alias `--merge-days`) bündelt alle
  Clips in *einem* Reel; der Ablageordner wird dann zum Tagesbereich
  (`2026-09-27_2026-09-30`).
- **Tripname aus dem Ingest-Ordner**: `ingest/Madeira 2026/` ergibt
  `output/Madeira-2026/` – ohne einen Titelparameter überall.
- **State-Datei** (`.tenor-state.json`) verhindert Doppelverarbeitung nach
  Neustarts. Schreiben ist atomar (fsync + eindeutige tmp-Datei) und es
  existiert eine `.bak`-Fassung: eine zerstörte State-Datei kostet nicht
  die ganze Historie. Verwaiste `*.part.mp4` aus abgebrochenen Läufen
  werden beim Start entfernt.

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
.venv/bin/pip install -e .          # stellt auch tenor, tenor-watcher, tenor-metadata bereit
# Alternativ nur Abhängigkeiten ohne Paket-Installation:
# .venv/bin/pip install -r requirements.txt
```

### 3. Konfiguration

`config.yaml` anpassen – oder für maschinenspezifische Overrides eine lokale
Datei aus der Vorlage anlegen (wird automatisch über `config.yaml` gemergt
und ist per `.gitignore` ausgeschlossen):

```bash
cp config.local.yaml.example config.local.yaml
```

Mindestens `ingest_dir`, `output_dir`, `music_dir` prüfen:

```yaml
ingest_dir: ~/video-cut/ingest
output_dir: ~/video-cut/output
music_dir: ~/video-cut/music
```

Werte lassen sich auch per Umgebungsvariable überschreiben – der Name ist der
Pfad in Großbuchstaben, mit `_` statt `.`:

```bash
TENOR_INGEST_DIR=/mnt/usb/clips TENOR_VIDEO_FPS=24 python -m src.pipeline --dry-run
TENOR_CUT_FALLBACK_INTERVAL=2.5 python -m src.watcher      # Fallback-Takt
TENOR_CONFIG=./config.yaml python -m src.watcher           # andere Config-Datei
```

Priorität (steigend): Defaults → `config.yaml` → CLI-Flags → `TENOR_*`.

Der Export und der Ton stecken in `video:`:

| Key | Default | Wirkung |
| --- | --- | --- |
| `fit_mode` | `crop` | Rahmung nicht-9:16-Clips (`TENOR_VIDEO_FIT_MODE` / `--fit`): `crop` (mittig zuschneiden), `blur` (ganzes Bild über weichgezeichnetem, abgedunkeltem Hintergrund), `pad` (ganzes Bild mit schwarzen Balken) |
| `audio` | `false` | `true` nimmt den **Originalton der Rohclips** mit (mit Musik gedämpft auf `original_gain`) |
| `original_gain` | `0.25` | Tonpegel des Originaltons, wenn Musik darunter liegt |
| `audio_codec` | `aac` | AAC – das einzige Tonformat, das Instagram/WhatsApp verlässlich annehmen |
| `audio_bitrate` | `192k` | Ton-Bitrate |
| `audio_sample_rate` | `44100` | Abtastrate im Ziel; Quellen werden darauf normalisiert |
| `audio_nbytes` | `2` | 2 = 16 bit |

Per CLI: `--audio` erzwingt Ton, `--no-audio` erzwingt stumm – beide schlagen
`video.audio` aus der Config. Ob Musik **im Reel** landet, entscheidet allein
`-m`: mit `-m <datei>` oder `-m auto` wird sie untergemischt, ohne `-m` steuert
die Musikquelle nur den **Schnitt**.

Die Musik im fertigen Reel steckt in `music:`:

| Key | Default | Wirkung |
| --- | --- | --- |
| `file` | `""` | feste Musikdatei, die `-m auto` verwendet (leer = bei `-m auto` kein Ton) |
| `gain` | `1.0` | Pegel der Musik |
| `fade_in` | `0.0` | Einblenden am Reel-Anfang (Sekunden, 0 = hart) |
| `fade_out` | `1.5` | Ausblenden am Reel-Ende (Sekunden) |

Kürzere Tracks loopen bis über die Reellänge, längere werden darauf geschnitten –
der Ton darf nicht vor dem letzten Bild verstummen.

Der Grading-Look steckt komplett in `style:`. Der eingecheckte Default ist
`preset: natural` (dezent, lebendig, tiefe Schwarztöne): `gamma: 1.0`,
`contrast: 0.02`, `black_lift: 0.0`, `black_toe: 1.0`, `highlight_ceiling: 1.0`,
`saturation: 1.03`, alle Farb-/Teal-/Skin-Werte `0.0`. Die folgende Tabelle
zeigt die Werte des **`cinematic` (Dark-Film)** Presets, das per
`--grade cinematic` oder `style.preset: cinematic` aktiviert wird:

| Key | natural (Default) | cinematic | Wirkung |
| --- | --- | --- | --- |
| `gamma` | `1.0` | `0.94` | Belichtung: **unter** 1.0 unterbelichtet (düster), über 1.0 hellt auf |
| `contrast` | `0.02` | `0.18` | Stärke der S-Kurve |
| `black_lift` | `0.0` | `0.015` | matter, leicht angehobener Schwarzpunkt |
| `black_toe` | `1.0` | `2.0` | Auslaufkurve der Schatten |
| `highlight_ceiling` | `1.0` | `0.94` | Highlight-Roll-Off, verhindert ausgebrannte Himmel |
| `saturation` | `1.03` | `0.92` | Sättigung |
| `shadow_desaturate` | `0.0` | `0.10` | Schatten stärker entsättigen |
| `teal_red/green/blue` | `0 / 0 / 0` | `-0.004 / 0.008 / 0.018` | Cine-Teal in den Schatten |
| `highlight_cool` | `0.0` | `0.008` | Lichter kühlen |
| `skin_damp` | `0.0` | `0.005` | Haut-/Erdtöne in den Mitteltönen dämpfen |

Presets: `natural` (Default), `cinematic` (Dark-Film) und `original` (kein
Filter). Einzelne Keys in `style:` überschreiben den Preset-Sockel; Werte, die
exakt dem `natural`-Sockel entsprechen, werden bei einem anderen Preset
ignoriert (damit die natürlichen Defaults den Dark-Look nicht aufweichen).
`--grade <preset>` (bzw. das `preset`-Argument) übernimmt die Preset-Werte
unverändert und ignoriert die Keys aus `style:`.

`style.text` steuert die Typografie (`anchor_x: 900`, `y_title: 1445`,
`safe_bottom: 1540`, Schriftgrößen, Tracking, Shadow).

Das Qualitäts-Gate steckt in `quality:` – `enabled: false` schaltet es
vollständig ab, dann läuft es exakt wie früher:

| Key | Default | Wirkung |
| --- | --- | --- |
| `sample_fps` | `4.0` | Abtastrate des Graustufen-Strips (Frames/Sekunde) |
| `edge_trim` | `0.25` | erste/letzte Viertelsekunde eines Clips aussparen |
| `min_sharpness` | `2500` | Laplacian-Varianz; fängt unscharfe Frames |
| `min_luma` / `max_luma` | `35` / `242` | schwarze bzw. ausgebrannte Frames |
| `max_black_ratio` / `max_white_ratio` | `0.35` / `0.25` | rein schwarze bzw. ausgefressene Flächen |
| `duplicate_threshold` | `20` | Signatur-Abstand, unter dem es eine Wiederholung ist |
| `duplicate_memory` | `3` | gegen wie viele zuletzt gezeigte Cuts verglichen wird |
| `search_offsets` | `[0.3, -0.3, 0.6, …]` | Sekunden, um die ein Fenster verschoben wird |
| `drop_duplicates` | `true` | echte Wiederholung verwerfen statt zu verschieben |
| `keep_imperfect` | `true` | unsauberes Bild behalten statt zu verwerfen |
| `cache_dir` | `~/video-cut/.cache/quality` | Strip-Cache |

Unter- und Obergrenzen mit `0` sind aus; bei `max_luma` und den Anteilen ist
der ausgeschaltete Wert `255` bzw. `1.0`.

## CLI-Nutzung

Nach Installation via `pip install -e .` stehen direkt die Befehle `tenor`,
`tenor-watcher` und `tenor-metadata` bereit (alternativ `python -m src.pipeline` usw.):

```bash
# Kompletter Ordner, Beat-Matching über die neueste MP3 im music_dir.
# Ohne -t wird der Ordnername unterhalb von ingest_dir zum Tripnamen.
.venv/bin/python -m src.pipeline -i ~/video-cut/ingest

# Tripname und Ausgabeordner explizit
.venv/bin/python -m src.pipeline -i ~/video-cut/ingest -t "Madeira 2026"

# Einzelne Datei, explizite Musik
.venv/bin/python -m src.pipeline -i clip.MOV -o out/ -t "Madeira" -m song.mp3

# Musik aus music.file (config.yaml)
.venv/bin/python -m src.pipeline -i clip.MOV -m auto

# Mit Originalton der Clips - Musik liegt gedämpft darunter
.venv/bin/python -m src.pipeline -i clip.MOV -m song.mp3 --audio

# Querformat-Clips ohne Zuschneiden über Blur-Hintergrund + Dark-Film-Grading + schnelles Tempo
.venv/bin/python -m src.pipeline -i ~/video-cut/ingest --fit blur --grade cinematic --pace fast

# Feste BPM, maximale Reeldauer (30 s) und alle Tage in einem Reel bündeln
.venv/bin/python -m src.pipeline -i ~/video-cut/ingest --bpm 128 --duration 30 --single-reel

# Nur den Schnittplan ansehen, kein Export (legt nichts an)
.venv/bin/python -m src.pipeline -i ~/video-cut/ingest --dry-run

# Version anzeigen
.venv/bin/python -m src.pipeline --version

# Metadaten inspizieren (Debug)
.venv/bin/python -m src.metadata -i ~/video-cut/ingest

# Watcher manuell starten
.venv/bin/python -m src.watcher --config config.yaml
```

Wichtige Flags der Pipeline: `--no-cover`, `--no-geo` (ohne Netzwerk),
`--limit N`, `--duration SEC`, `--grade natural|original|cinematic`,
`--fit crop|blur|pad`, `--pace fast|medium|slow`, `--bpm N`, `--json`,
`--delete-after` (Rohclips nach Export löschen), `--audio` / `--no-audio`
(Ton an/aus), `--dry-run` und `-V` / `--version`. Mit `--single-reel`
(Alias `--merge-days`) werden Clips aus mehreren Aufnahmetagen bewusst in
*einem* Reel gebündelt statt pro Tag getrennt (nützlich bei Mehrfachauswahl über
mehrere Tage). `--json` meldet mit `beat_source` die tatsächlich verwendete
Beat-Quelle und mit `beat_note` eine Erklärung, falls die gewünschte Quelle
nicht nutzbar war.

### Ausgabe

Pro Aufnahmetag ein Reel, inklusive Cover:

```
output/Madeira-2026/2026-09-11/
├── 2026-09-28_Madeira-2026_134838.mp4   H.264, yuv420p, 30 fps, 1080x1920
│                                        (stumm; Ton nur mit -m/--audio)
└── 2026-09-28_Madeira-2026_134838.jpg   Cover-Frame bei Sekunde 0.5
```

`--json` listet alle erzeugten Reels:

```json
{
  "outputs": [".../2026-09-11/….mp4", ".../2026-09-12/….mp4"],
  "covers":  [".../2026-09-11/….jpg", ".../2026-09-12/….jpg"],
  "cuts": 5, "duration": 7.0, "trip": "Madeira 2026",
  "beat_source": "beats:nastelbom-cinematic-485889.mp3",
  "beat_note": "Spotify blockiert die Audio-Analysis für diese App (HTTP 403)"
}
```

`beat_source` nennt die tatsächlich benutzte Beat-Quelle, `beat_note` erklärt
den Abstieg, falls die Wunschquelle nicht griff (`""` = alles wie gewünscht).

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
sudo loginctl enable-linger "$USER"     # sonst kein Start nach dem Abmelden
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

## Docker

```bash
# Image bauen
docker build -t tenor:latest .

# Schnittplan prüfen (Dry-Run)
docker run --rm \
  -v ~/video-cut/ingest:/data/ingest:ro \
  -v ~/video-cut/output:/data/output \
  -v ~/video-cut/music:/data/music:ro \
  tenor:latest -i /data/ingest --dry-run

# Optional mit eigener Config und Spotify-Umgebungsdatei
docker run --rm \
  -v ~/video-cut/ingest:/data/ingest:ro \
  -v ~/video-cut/output:/data/output \
  -v ~/video-cut/music:/data/music:ro \
  -v ~/video-cut/config.local.yaml:/home/tenor/config.yaml:ro \
  --env-file ~/.config/tenor/spotify.env \
  tenor:latest -i /data/ingest --fit blur --grade cinematic
```

## Projektstruktur

```
tenor/
├── .gitignore                # ignoriert .venv, Roh-/Audioformate, Render-Artefakte, Output
├── Dockerfile                # Non-Root-Container (Python 3.12 + ffmpeg + fonts-roboto)
├── justfile                  # Entwicklungs- und Docker-Rezepte
├── README.md
├── requirements.txt          # gepinnte Abhängigkeiten
├── pyproject.toml            # Packaging, Ruff/MyPy/Pytest-Konfiguration
├── config.yaml               # Pfade, Grading, Typografie, Watcher, Spotify (generisch)
├── config.local.yaml.example # Vorlage für lokale Overrides (nach config.local.yaml kopieren)
├── spotify.env.example       # Vorlage für Spotify-Credentials
├── src/
│   ├── __init__.py           # Konfigurations-Infrastruktur (Defaults + Deep-Merge + Env)
│   ├── pipeline.py           # Beat-Matching, Schnittplan, Rendern, Export, Cover, CLI
│   ├── metadata.py           # ffprobe, ISO-6709, Reverse-Geocoding, Sortierung, CLI
│   ├── quality.py            # Bildqualitäts-Gate: Strip-Analyse, Fenster-Planung, Cache
│   ├── spotify_beats.py      # Spotify Audio-Analysis (optional, thread-sicherer Token-Cache)
│   ├── styling.py            # LUT-Grading, Center-Crop, Font-Auflösung, Text-Overlay
│   └── watcher.py            # watchdog-Ingest mit Debounce + Batch-Fenster
├── tests/                    # pytest-Suite (23 Testmodule, siehe "Tests")
└── systemd/
    ├── tenor-watcher.service
    └── tenor-watcher.path
```

> **Lokale Overrides:** Werte, die nicht ins Repo gehören (echte Ingest-/
> Output-Pfade, Nextcloud-Befehl, Musik/Track), kommen in `config.local.yaml`
> neben der `config.yaml`. Sie wird automatisch darüber gemergt und ist per
> `.gitignore` ausgeschlossen. Priorität (steigend):
> Defaults < `config.yaml` < `config.local.yaml` < `--config`/CLI < `TENOR_*`.

### Datenfluss im Detail

1. **`metadata.py`** – `ffprobe -print_format json` liefert Format- und
   Stream-Tags. Der Aufnahmezeitpunkt kommt aus den QuickTime-Tags
   (`com.apple.quicktime.creationdate`), notfalls aus dem Datum im
   Location-Tag, notfalls mtime. `parse_coordinates()` versteht
   `+53.4702-006.1153+023.5/`, `+5328-00607/` und `53°28'12"N 6°06'55"W`.
   `Geocoder` löst Koordinaten über Nominatim auf und cacht sie in
   `.cache/geocoding.json` (inklusive Negativ-Caching bei Netzwerkfehlern).
2. **`pipeline.py`** – `trip_from_path()` liest den Tripnamen aus dem
   Ingest-Unterordner. `_plan_clip_cuts()` entscheidet, *wieviele* Cuts ein
   Clip liefert (aus dem Beat-Grid), `spread_windows()` verteilt sie über den
   ganzen Clip, und `quality.plan_windows()` verschiebt oder verwirft sie nach
   Bildqualität. Cuts unter `target_min` werden verworfen. Anschließend
   gruppiert `run()` das Material nach Aufnahmetag und ruft für jede Gruppe
   `export_reel()` auf.
3. **`quality.py`** – ein ffmpeg-Durchlauf pro Rohclip liefert einen
   Graustufen-Strip (4 fps, 90 px hoch). Daraus werden Schärfe, Luma,
   Schwarz-/Weißanteil und eine 16×16-Signatur pro Frame berechnet – und
   zwischengespeichert, sodass wiederholte Läufe nichts neu dekodieren.
4. **`styling.py`** – `center_crop_resize()` beschneidet auf exakt 9:16,
   `make_grader()` legt die LUT und die Farbstufe auf jeden Frame,
   `build_text_overlay()` rendert Titel/Unterzeile rechtsbündig mit Shadow.
5. **Export** – `write_videofile()` mit `libx264`, `yuv420p`, 30 fps;
   `video.audio` (Default `false`) steuert, ob der Originalton der Clips
   mitgeschrieben wird. Bei `true` wird pro Quellclip ein eigener
   Audio-Subclip geöffnet (`audio_fps` normalisiert, Codec `aac`), der
   stummgeschaltete Clips korrekt an der Zeitachse auslässt. Ist Musik bewusst
   gewählt, mischt `mix_music()` sie unter dieses Bild (Originalton auf
   `video.original_gain` gedämpft) – ohne gewählte Musik bleibt der Tonpfad
   unverändert. Danach das Cover via `ffmpeg -ss 0.5`.

## Fehlerbehandlung

| Situation | Verhalten |
| --- | --- |
| `ffprobe` schlägt fehl / Datei kaputt | Warnung, Datei wird über mtime einsortiert bzw. übersprungen |
| Clip scheitert beim Rendern | Nur dieser Cut wird verworfen, restliche Cuts rendern weiter |
| Kein Beat-Grid (Musik zu kurz/leise) | BPM-Fallback aus `cut.fallback_bpm` (Default 120) |
| Kein Spotify-Key / Track nicht gefunden | Stille auf den BPM-Fallback, kein Abbruch |
| Spotify blockiert Audio-Analysis (`HTTP 403`) | Klare Warnung im Log, `beat_note` in der JSON-Ausgabe; Beat-Quelle wird die lokale Datei (`spotify.track_file` bzw. neueste Datei aus `music_dir`), sonst BPM-Fallback |
| `video.audio: true`, aber Clip ohne Audiospur | Clip wird stumm exportiert, Warnung im Log (`has_audio_stream`) |
| Gewählte Musik fehlt oder ist unlesbar | Warnung im Log, Reel wird stumm exportiert – der Lauf bricht nicht ab |
| Musik und Originalton haben verschiedene Kanalzahl (Mono-Clip, Stereo-MP3) | `_match_audio_channels()` gleicht die Spuren an, sonst scheiterte `CompositeAudioClip` mit einem Broadcast-Fehler |
| Nominatim nicht erreichbar | Negativ gecacht, Lauf läuft ohne Ortsangabe weiter |
| Kein Font ladbar | Pillow-Defaultfont als letzte Instanz |
| Qualitätsanalyse schlägt fehl (ffmpeg, Timeout, kaputte Datei) | Gate wird für den Clip übersprungen, **kein Material geht verloren** |
| Durchgefallenes Fenster lässt sich nicht retten | Unsauberes Bild wird behalten (`quality.keep_imperfect`), echte Wiederholung verworfen (`quality.drop_duplicates`) |
| Export abbruch | `*.part.mp4` wird gelöscht, kein unfertiges Ergebnis |
| Ereignisse während des Schreibens | Debounce wartet auf stabile Dateigröße (max. 6× Debounce) |
| Batch-Lauf scheitert komplett | Dateien bleiben liegen, Retry nach `watcher.retry_backoff` (Standard 5 min), max. `retry_limit` Versuche |
| State-Datei beschädigt | `.bak`-Fassung wird geladen; notfalls Neuaufbau (dann einmaliges Neurendern) |
| `MemoryMax` erreicht | systemd killt und startet neu; unfertige `.part`-Dateien werden beim Start entfernt |
| `SIGTERM` während des Renderns | Lauf verworfen, Clips bleiben im Ingest und werden beim nächsten Start neu verarbeitet |

### Spotify-Beat-Matching (optional)

```bash
mkdir -p ~/.config/tenor
printf 'SPOTIFY_CLIENT_ID=...\nSPOTIFY_CLIENT_SECRET=...\n' > ~/.config/tenor/spotify.env
chmod 600 ~/.config/tenor/spotify.env
```

In `config.yaml`:

```yaml
spotify:
  enabled: true
  track_uri: "spotify:track:4cOdK2wGLETKBW3PvgPWqT"
  # Der Audio-Analysis-Endpoint liefert bei neuen Apps HTTP 403.
  # Dann diese Zeile auf die lokale MP3/M4A des Tracks zeigen lassen:
  # track_file: "/pfad/zum/track.mp3"
```

Oder einmalig auf der CLI: `python -m src.pipeline -m "spotify:track:<ID>"`.

> **Wichtig:** Seit dem 27.11.2024 gibt Spotify `/v1/audio-analysis` und
> `/v1/audio-features` nur noch an Apps mit *Extended Quota* heraus. Jede
> später registrierte App erhält dort dauerhaft `HTTP 403`. Das ist **nicht**
> per Konfiguration behebbar. `tenor` meldet das deshalb jetzt laut:

```text
Beat-Quelle: beats:nastelbom-cinematic-485889.mp3 (Spotify blockiert die Audio-Analysis für diese App (HTTP 403))
```

Geholt werden nur die Beat-Zeitpunkte, nicht die Audiodatei – eine Spotify-URI
ist damit **nie** die Tonquelle. Ist Spotify blockiert, wird stattdessen
`spotify.track_file` (bzw. die neueste Datei im `music_dir`) als Beat-Quelle
genutzt; nur wenn auch das fehlt, greift der BPM-Fallback. Auch diese
Automatik steuert ausschließlich den Schnittplan; untergemischt wird Musik nur
als **lokale Datei**, die bewusst gewählt wurde – etwa per `-m <datei>`
oder `-m auto` (zeigt auf `music.file`). Originalton kommt über
`video.audio: true` bzw. `--audio` ins Reel, dann gedämpft unter der Musik
(siehe [Features](#features)).

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy src
```

Die Suite deckt vor allem die Fehler ab, die im Betrieb still gescheitert
sind – u. a. der Event-Handler, der den inotify-Thread killte, die State-Datei,
BPM-Fallback und die Cut-Positionen:

| Datei | Schwerpunkt |
| --- | --- |
| `test_config.py` | Config-Laden, Deep-Merge, `TENOR_*`-Env-Overrides |
| `test_pipeline_cli.py`, `test_pipeline_export_and_run.py`, `test_pipeline_name_collision.py`, `test_pipeline_performance.py` | Pipeline-CLI, Export/Run, Dateinamenskollisionen, 10-bit/ffprobe-Deduplizierung |
| `test_fit_mode.py`, `test_is_valid_video.py`, `test_rotation.py`, `test_output_name_fallback.py`, `test_merge_days.py` | Framing (`crop`/`blur`/`pad`), Video-Validierung, Rotation, Dateinamen-Fallback, `--single-reel` |
| `test_quality.py` | Kalibrierung des Qualitäts-Gates, Frame-Dekodierungs-Guards, "verliert nie Material" |
| `test_metadata.py`, `test_metadata_cli.py`, `test_metadata_validation.py` | ffprobe-Parsing, ISO-6709, Koordinaten-Validierung, CLI |
| `test_spotify_beats.py`, `test_spotify_beats_validation.py` | Spotify Audio-Analysis, Token-Cache, URI/Track-Validierung, Fehlerpfade |
| `test_styling.py`, `test_styling_and_pacing.py` | Grading-LUT, Presets, Typografie, Pacing |
| `test_watcher.py`, `test_watcher_rescan.py`, `test_watcher_shutdown.py` | Watcher-Ingest, Queue-Limit, Rescan-Fehlertoleranz, Shutdown |
| `test_stability.py` | Regressionen: Event-Handler, State-Datei, BPM-Fallback, Cut-Positionen |
| `test_nextcloud_scan.py` | Nextcloud-Scan (inkl. Command-Injection-Schutz) |

`ruff`/`mypy`-/`pytest`-Konfiguration steht in `pyproject.toml`; dieselben
Checks laufen in der CI.

`tests/test_quality.py` sichert zusätzlich die Dinge ab, die am echten
Material *kalibriert* und nicht hergeleitet sind:

- dass `quality.duplicate_threshold` in der gemessenen Lücke liegt
  (Burst 6,8–13,9 und zwei Cuts aus demselben Shot 7,4–8,0 fallen durch,
  verschiedene Szenen ab 24,7 bleiben)
- dass das Gate nie Material verliert, das es nicht beurteilen kann
  (kaputte Datei, Timeout, fehlendes ffmpeg, deaktiviertes Gate)
- dass die Fenster sich nie überlappen und nie über das Clip-Ende hinaus
  geplant werden, und dass ein einzelner Cut aus einem 3-s-Clip weiterhin
  zentriert bei 0,75–2,25 s liegt


## Exit-Codes

`0` = Reel geschrieben (oder `--dry-run` erfolgreich), `1` = kein Export
möglich, `2` = Konfigurations-/Aufruffehler.

## Lizenz & Mitwirken

- **Lizenz:** [MIT](LICENSE)
- **Mitwirken:** [CONTRIBUTING.md](CONTRIBUTING.md) und [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- **Sicherheit:** [SECURITY.md](SECURITY.md) (bitte über GitHub Private Vulnerability Reporting melden)
- **Änderungen:** [CHANGELOG.md](CHANGELOG.md)
