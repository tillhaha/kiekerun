# KiekeRun: Datenfluss

Setup: Claude ist der Coach, intervals.icu der Planer und die Analyse, KiekeRun der Laufband-Runner, die Garmin fenix 9 Pro die Uhr für draußen.

## Gesamtübersicht

```mermaid
flowchart TB
    subgraph Coach["Coaching"]
        CL["Claude (Coach)<br/>Trainingsplan, Workouts"]
    end

    subgraph IC["intervals.icu"]
        CAL["Kalender<br/>geplante Workouts"]
        ACT["Aktivitäten<br/>Analyse, Fitness, Load"]
        PROF["Athletenprofil<br/>Gewicht (icu_weight)"]
    end

    subgraph Draussen["Outdoor"]
        GA["Garmin fenix 9 Pro<br/>GPS, HR, Höhe"]
        GC["Garmin Connect"]
    end

    subgraph Hetzner["Hetzner / Coolify (Docker, Port 8000)"]
        BE["KiekeRun Backend (FastAPI)<br/>Login: KIEKERUN_USER / PASSWORD<br/>Key: INTERVALS_API_KEY"]
    end

    subgraph IPad["iPad im Browser: KiekeRun (index.html)"]
        UI["Plan → Run → Summary"]
        FB["Workout-Feedback<br/>RPE, Gefühl, Karbs, Notiz"]
    end

    subgraph Hardware["Hardware (Bluetooth)"]
        TM["Laufband<br/>FTMS"]
        HR["Herzfrequenz-Gurt<br/>HR Service"]
    end

    CL -- "1 Workouts planen" --> CAL
    CAL -- "2 heutiges Workout laden" --> BE
    BE -- "GET /api/planned" --> UI
    UI -- "3 Sollwerte Speed/Incline<br/>pro Block" --> TM
    TM -- "4 Ist-Speed/Incline" --> UI
    HR -- "5 Puls" --> UI
    UI --> FB
    FB -- "6 Log + Feedback" --> UI
    UI -- "7 POST /api/upload<br/>Log als JSON" --> BE
    PROF -- "Gewicht" --> BE
    BE -- "8 FIT-Datei + RPE, Gefühl,<br/>Karbs, Beschreibung" --> ACT
    ACT -- "gepaart mit dem Plan" --> CAL
    GA --> GC
    GC -- "Sync (Outdoor-Läufe)" --> ACT
    ACT -- "9 Ergebnisse lesen" --> CL
```

## Ablauf Treadmill-Einheit

```mermaid
sequenceDiagram
    autonumber
    actor T as Tito
    participant C as Claude
    participant I as intervals.icu
    participant B as Backend (FastAPI)
    participant U as KiekeRun (iPad)
    participant TM as Laufband
    participant H as HR-Gurt

    C->>I: Workout in den Kalender (Text mit "incline Npct")
    T->>U: Button "Today's workout"
    U->>B: GET /api/planned
    B->>I: GET /events (Tag, WORKOUT)
    I-->>B: Events
    B-->>U: Workout als JSON (Blöcke)
    T->>U: Start
    loop jede Sekunde
        U->>TM: Speed und Incline setzen (Bluetooth FTMS)
        TM-->>U: Ist-Werte
        H-->>U: Puls (Bluetooth)
        U->>U: Log: Events, HR, Blöcke
    end
    T->>U: Finish, Summary
    T->>U: Workout-Feedback (RPE, Gefühl, Karbs, Notiz per Sprache)
    T->>U: Upload to intervals.icu
    U->>B: POST /api/upload (Log + Feedback + paired_event_id)
    B->>I: GET Athletenprofil (Gewicht)
    B->>B: Kalorien schätzen, Vertikalgeschwindigkeit pro Block, FIT bauen
    B->>I: POST /activities (FIT, device_name KiekeRun, mit Plan gepaart)
    B->>I: PUT /activity/{id} (icu_rpe, feel, carbs_ingested, description)
    I-->>B: Aktivität
    B-->>U: Ergebnis, Link zur Aktivität
    C->>I: Auswertung lesen, nächste Einheit anpassen
```

## Datenquellen und was daraus wird

| Daten | Quelle | Weg | Ziel in intervals.icu |
|---|---|---|---|
| Geplantes Workout | Claude | Kalender-Event, Textformat mit `incline Npct` | Kalender (Kategorie WORKOUT, `external_id` `tcoach-…`) |
| Sollwerte Speed/Incline | Workout-Blöcke | KiekeRun sendet per Bluetooth FTMS ans Laufband | nur im Log |
| Ist-Speed, Incline, Distanz | Laufband | Bluetooth FTMS, 1 Hz | FIT: Geschwindigkeit, Distanz |
| Höhenmeter | berechnet aus Speed und Incline | Backend | FIT: Höhe, Steigung (Elevation gain) |
| Puls | HR-Gurt | Bluetooth, direkt im Browser | FIT: Herzfrequenz |
| Kalorien | berechnet (ACSM-Formel) | Backend, Gewicht aus intervals-Profil oder `BODY_WEIGHT_KG` | FIT `total_calories`, Beschreibung |
| Vertikalgeschwindigkeit pro Block | berechnet | Backend | Beschreibung der Aktivität |
| RPE | Tito | Feedback-Popup | `icu_rpe` |
| Gefühl | Tito | Feedback-Popup | `feel` |
| Kohlenhydrate | Tito | Feedback-Popup (Summe) | `carbs_ingested` |
| Notiz | Tito (Sprache) | Feedback-Popup | `description` |
| Outdoor-Lauf | Garmin fenix 9 Pro | Garmin Connect, Sync nach intervals.icu | Aktivität |

## Komponenten und Zugang

| Komponente | Aufgabe | Zugang / Geheimnisse |
|---|---|---|
| KiekeRun (`index.html`) | Runner, Bluetooth, Feedback, Log | Browser, HTTP-Basic-Login |
| Backend (`server/app.py`) | Brücke zu intervals.icu, FIT-Bau, Kalorien | `KIEKERUN_USER`, `KIEKERUN_PASSWORD`, `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`, optional `TZ_NAME`, `BODY_WEIGHT_KG` |
| intervals.icu API | Kalender, Aktivitäten, Profil | Basic-Auth, Benutzer `API_KEY` |
| Hosting | Docker auf Coolify (Hetzner), Port 8000 | Build-Pack Dockerfile, Deployment per GitHub-Push auf `main` |

## Hinweise

- Der Garmin-Pfad (Garmin Connect nach intervals.icu) liegt außerhalb dieses Repos und ist nur der Vollständigkeit halber eingezeichnet.
- Laufband-Einheiten werden nur über KiekeRun aufgezeichnet, nicht über die Garmin.
- Die Aktivität bleibt als `VirtualRun` angelegt. Ein Wechsel auf `Run` bräuchte einen zusätzlichen API-Aufruf.
- Läuft das Upload-Update (RPE usw.) in einen Fehler, bleibt die Aktivität trotzdem erhalten, der Fehler wird nur gemeldet.
- Doppelte Uploads erkennt intervals.icu an der `external_id` und legt keine zweite Aktivität an.
