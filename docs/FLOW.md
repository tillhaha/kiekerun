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

 Claude (Coach)
   |
   | 1b. Workouts pushen (Outdoor-Einheiten)
   v
 +---------------+  Push   +-----------------+  Sync  +--------------------+
 | intervals.icu | ------> | Garmin Connect  | -----> | Garmin fenix 9 Pro |
 | (Kalender)    |         |                 |        | (Workout auf Uhr)  |
 +---------------+         +-----------------+        +---------+----------+
        ^                           ^                           |
        |   Aktivität (Sync)        |    Lauf aufgezeichnet     |
        +---------------------------+---------------------------+

 9. Claude liest die Ergebnisse (alle Quellen) aus intervals.icu und passt den Plan an:

 Claude (claude.ai, Coach)
   |  Connector-URL: https://<domain>/mcp/<MCP_TOKEN>   (nur lesen)
   v
 +-----------------------------------------------------------+
 | Backend (/mcp)                                            |
 |   get_activities | get_activity | get_wellness |          |
 |   get_plan_vs_actual                                      |
 +--+--------------------------------------------------------+
    | GET /activities, /activity/{id}, /wellness, /events
    v
 +---------------+
 | intervals.icu |   (KiekeRun-Uploads + Garmin Connect, kein Strava-Import mehr)
 +---------------+
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
| Outdoor-Workout (geplant) | Claude | intervals.icu-Kalender, Garmin Connect, Uhr (Push, angenommen) | fenix 9 Pro |
| Outdoor-Lauf (Ist) | Garmin fenix 9 Pro | Garmin Connect, Sync | Aktivität |
| Alle Daten zurück zum Coach | intervals.icu | Backend-Connector `/mcp/<MCP_TOKEN>` (nur lesen) | Claude |

## Hinweise

- Der Garmin-Pfad (Push der Workouts auf die Uhr und Sync der Aktivität zurück) liegt außerhalb dieses Repos. Der genaue Weg des Pushes (hier über intervals.icu und Garmin Connect) ist angenommen.
- Laufband-Einheiten werden nur über KiekeRun aufgezeichnet, nicht über die Garmin.
- Die Aktivität bleibt als `VirtualRun` angelegt.
- Schlägt das Update (RPE usw.) fehl, bleibt die Aktivität trotzdem erhalten, der Fehler wird gemeldet.
- Doppelte Uploads erkennt intervals.icu an der `external_id`, es entsteht keine zweite Aktivität.
