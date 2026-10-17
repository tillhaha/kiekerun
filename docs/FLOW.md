# KiekeRun: Datenfluss

Stand: 16.10.2026

Claude = Coach, intervals.icu = zentrale Datenablage, Planer und Analyse, KiekeRun = Laufband-Runner,
Garmin fenix 9 Pro = Uhr für draußen. Strava ist von intervals.icu getrennt, es gibt keinen Strava-Import mehr.

## Gesamtübersicht

```
                         +------------------------------+
                         | Claude (claude.ai, Coach)    |
                         | Projekt "Ultrarunning"       |
                         +------+----------------^------+
                                |                |
              1. Workouts planen|                | 9. Ergebnisse lesen (nur lesen)
                (Kalender)      |                |    Connector-URL /mcp/<MCP_TOKEN>
                                v                |
 +--------------------------------------------------------------------------+
 | intervals.icu  (zentrale Ablage, alle Quellen)                           |
 |   Kalender (geplant) | Aktivitäten | Wellness, CTL/ATL | Profil (Gewicht) |
 +---^--------+-----------------------------^-----------^--------+----------+
     |        |                             |           |        |
     |        | 2. Plan auf die Uhr         | 8. FIT +  |        | 6b. GET Plan,
     |        |    (Garmin-Sync)            |    Feed-  |        |     Aktivitäten,
     |        v                             |    back   |        |     Wellness
 +---+----------------+     Aktivität       |           |  +-----v-----------------+
 | Garmin Connect     | --------------------+           |  | Backend: /mcp          |
 +---^--------+-------+                                 |  |  get_activities        |
     |        |                                         |  |  get_activity          |
     |        v                                         |  |  get_wellness          |
     |  +--------------------+                          |  |  get_plan_vs_actual    |
     |  | Garmin fenix 9 Pro |                          |  +------------------------+
     |  | Outdoor, GPS, HR   |                          |
     |  +--------------------+                          |
     +--- Lauf aufgezeichnet                            |
                                                        |
 +------------------------------------------------------+-----------------+
 | Backend (FastAPI, Docker auf Coolify/Hetzner, Port 8000)               |
 |   /          Seite, Basic-Login        /api/planned   Plan lesen/schreiben |
 |   /api/upload FIT bauen, Kalorien      /mcp/<token>  Coach-Connector       |
 |   Schutz: Login, Rate-Limit + Sperre pro IP, MCP_TOKEN in der URL          |
 +---^------------------------------------------------+----------------------+
     | 3. Workout als JSON                            | 7. POST /api/upload
     v                                                |    (Log + Feedback)
 +--------------------------------------------------------------------------+
 | KiekeRun auf dem iPad (index.html)                                       |
 |   1. Plan -> 2. Run -> 3. Summary                                        |
 |   Run: Pace, Puls, Speed, Incline mit Sollwerten | Workout-Feedback       |
 +---+--------------------------------^-------------------------------------+
     | 4. Speed/Incline               | 5. Ist-Werte + Puls
     |    (Bluetooth FTMS)            |    (Bluetooth)
     v                                |
 +----------------+         +---------+-------+
 | Laufband       |         | HR-Gurt         |
 +----------------+         +-----------------+
```

Nummerierung: 1 bis 8 beschreiben den Weg einer Laufband-Einheit, 9 das Zurücklesen durch den Coach.
Outdoor-Einheiten laufen ohne KiekeRun: Kalender, Garmin Connect, Uhr, und die Aktivität zurück nach intervals.icu.

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
```

## Ablauf: Coach liest die Ergebnisse

```
 Claude (claude.ai)       Backend /mcp/<MCP_TOKEN>          intervals.icu
   |                              |                              |
   |-- tools/call --------------->|  Token prüfen,               |
   |   get_plan_vs_actual         |  Rate-Limit, Sperre          |
   |                              |-- GET /events -------------->|
   |                              |-- GET /activities ---------->|
   |                              |<-- Plan + Aktivitäten -------|
   |<-- kompakte Übersicht -------|   (Pace, HR, Höhe, Load,     |
   |    pro Tag: Plan vs. Ist     |    RPE, Gefühl, Karbs, Notiz,|
   |                              |    Quelle)                   |
   |-- get_wellness ------------->|-- GET /wellness ------------>|
   |<-- CTL, ATL, Form, HRV, -----|                              |
   |    Ruhepuls, Schlaf          |                              |
   |-- get_activity(id) --------->|-- GET /activity/{id} ------->|
   |<-- Intervalle, HR-Zonen -----|                              |
   |                              |                              |
   |  Auswertung -> nächste Einheit planen (zurück zu Schritt 1)
```

## Datenquellen und Ziele

| Daten | Quelle | Weg | Ziel |
|---|---|---|---|
| Geplantes Workout | Claude | Kalender-Event, Schritte als `Beschreibung 30 min @ 5.5 km/h (10:55 min/km) @ 15% incline`; Dauer und Distanz als eigene Felder | intervals.icu-Kalender (WORKOUT, `external_id` `tcoach-...`) |
| Geplantes Outdoor-Workout | Claude | intervals.icu-Kalender, Garmin-Sync | fenix 9 Pro (Push, Weg angenommen) |
| Nutrition-Plan (Zeit, Karbs, Flüssigkeit, Notiz) | Claude | Abschnitt `Fuel plan` in der Event-Beschreibung (`@ 20:00 · 0.5 Gel-Flask · 55 g carbs · 75 ml · V4-Mix`, Mengen als Anteil einer vollen Flask), Zeiten in Workout-Zeit | KiekeRun: Übersicht vor dem Start, Fuel-Box mit Erinnerung im Run; "Taken" schreibt ins Karbs-Log |
| Soll Speed/Incline | Workout-Blöcke | KiekeRun per Bluetooth FTMS | Laufband, nur im Log |
| Ist Speed, Incline, Distanz | Laufband | Bluetooth FTMS, 1 Hz | FIT |
| Höhenmeter | berechnet (Speed, Incline) | Backend | FIT (Höhe, Steigung) |
| Puls (Laufband) | HR-Gurt | Bluetooth direkt im Browser | FIT |
| Kalorien | berechnet (ACSM) | Backend, Gewicht aus Profil oder `BODY_WEIGHT_KG` | FIT + Beschreibung |
| Vertikalgeschwindigkeit pro Block | berechnet | Backend | Beschreibung |
| RPE, Gefühl, Karbs, Notiz | Tito | Feedback-Popup | `icu_rpe`, `feel`, `carbs_ingested`, `description` |
| Outdoor-Lauf (Ist) | Garmin fenix 9 Pro | Garmin Connect, Sync | intervals.icu-Aktivität |
| Wellness (HRV, Schlaf, Ruhepuls) | Garmin | Garmin Connect, Sync | intervals.icu-Wellness |
| Alle Daten zurück zum Coach | intervals.icu | Backend-Connector `/mcp/<MCP_TOKEN>`, lesend | Claude |
| Plan vom Coach direkt in den Kalender | Claude | Connector-Tools `save_treadmill_workout`, `save_planned_workout`, `delete_planned_workout` (nur mit `MCP_ALLOW_WRITE=1`) | intervals.icu-Kalender |

## Zugang und Schutz

| Zugang | Schutz |
|---|---|
| KiekeRun-Seite und `/api` | HTTP-Basic-Login (`KIEKERUN_USER`, `KIEKERUN_PASSWORD`), ohne gesetzte Variablen gesperrt (503) |
| Coach-Connector `/mcp/<MCP_TOKEN>` | geheime URL (mind. 16 Zeichen), falscher Token liefert 404. Standard nur lesen; Schreiben (nur geplante Workouts, löschen nur eigene, 30 pro Stunde) braucht `MCP_ALLOW_WRITE=1` |
| Beide | Rate-Limit: 10 Fehlversuche in 15 Minuten sperren die Adresse für 15 Minuten (429); Connector auf 60 Anfragen pro Minute begrenzt |
| intervals.icu | API-Key nur in Coolify (`INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`), nie im Chat |
| Client-Adresse | aus `X-Forwarded-For`, `TRUSTED_PROXY_HOPS` (Standard 1) |

Mögliche nächste Schritte: OAuth statt geheimer URL für den Connector (Login, Token mit Ablauf, widerrufbar),
Strava-Upload der Laufband-Einheiten direkt aus KiekeRun.

## Hinweise

- Laufband-Einheiten werden nur über KiekeRun aufgezeichnet, nicht über die Garmin.
- Der Weg des Workout-Pushes zur Garmin (über intervals.icu und Garmin Connect) ist angenommen und liegt außerhalb dieses Repos.
- Die Aktivität bleibt als `VirtualRun` angelegt.
- Schlägt das Update (RPE usw.) fehl, bleibt die Aktivität trotzdem erhalten, der Fehler wird gemeldet.
- Doppelte Uploads erkennt intervals.icu an der `external_id`, es entsteht keine zweite Aktivität.
- Garmin-Läufe sind nicht automatisch mit dem Plan gepaart. Dann zeigt `get_plan_vs_actual` "no paired activity", die Aktivität steht aber am selben Tag.
- Der Connector wurde gegen das echte Konto geprüft: `get_activities` liefert Garmin-Läufe mit Pace, Puls, Höhe und Load.
