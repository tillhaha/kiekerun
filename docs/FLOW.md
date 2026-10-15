# KiekeRun: Datenfluss

Claude = Coach, intervals.icu = Planer + Analyse, KiekeRun = Laufband-Runner, Garmin fenix 9 Pro = Uhr für draußen.

## Gesamtübersicht

```
 +------------------+
 | Claude (Coach)   |
 +--------+---------+
          | 1. Workouts planen (Kalender-Events)
          v
 +-----------------------------------------------------------+
 | intervals.icu                                             |
 |   Kalender (geplant) | Aktivitäten (Analyse) | Profil     |
 +--^-------------------+-----------^-----------+------+-----+
    |                               |                  |
    | 2. GET /events (heute)        | 8. FIT + RPE,    | Gewicht
    |                               |    Gefühl, Karbs,|
    |                               |    Beschreibung  |
 +--+-------------------------------+------------------v-----+
 | Backend (FastAPI, Docker auf Coolify/Hetzner, Port 8000)  |
 |   Login: KIEKERUN_USER / KIEKERUN_PASSWORD                |
 |   Key:   INTERVALS_API_KEY, INTERVALS_ATHLETE_ID          |
 +--+-------------------------------^------------------------+
    | 3. Workout als JSON           | 7. POST /api/upload
    v                               |    (Log + Feedback)
 +-----------------------------------------------------------+
 | KiekeRun auf dem iPad (index.html)                        |
 |   1. Plan -> 2. Run -> 3. Summary                         |
 |   Workout-Feedback: RPE, Gefühl, Karbs, Notiz (Sprache)   |
 +--+---------------------------^----------------------------+
    | 4. Speed/Incline          | 5. Ist-Werte + Puls
    |    (Bluetooth FTMS)       |    (Bluetooth)
    v                           |
 +-----------------+      +-----+-----------+
 | Laufband        |      | HR-Gurt         |
 +-----------------+      +-----------------+


 Outdoor (getrennt von KiekeRun):

 +--------------------+      +-----------------+      +---------------+
 | Garmin fenix 9 Pro | ---> | Garmin Connect  | ---> | intervals.icu |
 +--------------------+      +-----------------+      +---------------+
                                  (Sync, angenommen)

 9. Claude liest die Ergebnisse aus intervals.icu und passt den Plan an.
```

## Ablauf einer Laufband-Einheit

```
 Claude        intervals.icu     Backend          KiekeRun (iPad)     Laufband / HR
   |                |               |                    |                  |
   |-- Workout ---->|               |                    |                  |
   |   planen       |               |                    |                  |
   |                |               |<-- GET /planned ---|  (Button "Today")|
   |                |<-- /events ---|                    |                  |
   |                |--- Events --->|                    |                  |
   |                |               |--- Workout JSON -->|                  |
   |                |               |                    |-- Start -------->|
   |                |               |                    |<- Speed/Incline -|  jede Sekunde
   |                |               |                    |<- Puls ----------|
   |                |               |                    |  (Log mitschreiben)
   |                |               |                    |                  |
   |                |               |                    |  Finish, Summary, Feedback
   |                |               |<-- POST /upload ---|  (Log + RPE, Gefühl,
   |                |<- Profil -----|    + Plan-ID       |   Karbs, Notiz)
   |                |-- Gewicht --->|                    |
   |                |               | Kalorien schätzen, |
   |                |               | Vertikalgeschw.,   |
   |                |               | FIT bauen          |
   |                |<- POST -------|                    |
   |                |   /activities |                    |
   |                |<- PUT --------|                    |
   |                |   /activity   |                    |
   |                |               |--- Ergebnis ------>|
   |<- Auswertung --|               |                    |
```

## Datenquellen und Ziele

| Daten | Quelle | Weg | Ziel in intervals.icu |
|---|---|---|---|
| Geplantes Workout | Claude | Kalender-Event, Text mit `incline Npct` | Kalender (WORKOUT, `external_id` `tcoach-...`) |
| Soll Speed/Incline | Workout-Blöcke | KiekeRun per Bluetooth FTMS ans Laufband | nur im Log |
| Ist Speed, Incline, Distanz | Laufband | Bluetooth FTMS, 1 Hz | FIT |
| Höhenmeter | berechnet (Speed, Incline) | Backend | FIT (Höhe, Steigung) |
| Puls | HR-Gurt | Bluetooth direkt im Browser | FIT |
| Kalorien | berechnet (ACSM) | Backend, Gewicht aus Profil oder `BODY_WEIGHT_KG` | FIT + Beschreibung |
| Vertikalgeschwindigkeit pro Block | berechnet | Backend | Beschreibung |
| RPE | Tito | Feedback-Popup | `icu_rpe` |
| Gefühl | Tito | Feedback-Popup | `feel` |
| Kohlenhydrate | Tito | Feedback-Popup (Summe) | `carbs_ingested` |
| Notiz | Tito (Sprache) | Feedback-Popup | `description` |
| Outdoor-Lauf | Garmin fenix 9 Pro | Garmin Connect, Sync | Aktivität |

## Hinweise

- Der Garmin-Pfad liegt außerhalb dieses Repos und ist nur zur Vollständigkeit eingezeichnet.
- Laufband-Einheiten werden nur über KiekeRun aufgezeichnet, nicht über die Garmin.
- Die Aktivität bleibt als `VirtualRun` angelegt.
- Schlägt das Update (RPE usw.) fehl, bleibt die Aktivität trotzdem erhalten, der Fehler wird gemeldet.
- Doppelte Uploads erkennt intervals.icu an der `external_id`, es entsteht keine zweite Aktivität.
