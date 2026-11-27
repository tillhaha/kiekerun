"""Mat workouts (no treadmill): Muscular Endurance (ME, WO 1-14) and Core 15.

A planned calendar entry carries a one-line marker in its description, e.g.
    KiekeRun: me wo=5 sets=5
    KiekeRun: core15 rounds=3
The page turns the marker into timed steps (everything runs on timers). The progression lives here, so
the planner (Claude) only writes the marker, and the page can rebuild the exact same session.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

KINDS = ("me", "core15")
ME_REPS = 10

# WO: (default sets, load, rest between sets s, rest between right/left s, rest between exercises s, note)
ME_WO = {
    1: (4, "Bodyweight", 60, 30, 60, "1 circuit; 4-6 sets possible"),
    2: (4, "Bodyweight", 60, 30, 60, "as WO 1"),
    3: (6, "Bodyweight", 45, 30, 60, "more density"),
    4: (5, "+10 % BW (vest)", 60, 30, 60, "adds goblet squat + kettlebell swing"),
    5: (6, "+10 % BW", 45, 30, 90, ""),
    6: (6, "+10 % BW", 40, 30, 60, ""),
    7: (6, "+10 % BW", 30, 30, 60, ""),
    8: (8, "+10 % BW", 45, 30, 60, "volume bump"),
    9: (6, "+15 % BW", 40, 30, 45, ""),
    10: (8, "+15 % BW", 45, 30, 30, ""),
    11: (8, "+15 % BW", 30, 20, 30, ""),
    12: (8, "+15 % BW", 15, 15, 20, ""),
    13: (8, "+15 % BW", 10, 10, 10, "max density"),
    14: (8, "+15 % BW", 10, 10, 10, "as WO 13"),
}
# name, seconds per rep (the book tempo doubled: 20 s per set of 10), unilateral, cue, first WO
ME_EX = [
    ("Split jump squat", 2.0, False, "Alternate legs in the air, soft landing", 1),
    ("Squat jump", 1.5, False, "Moderate height in the first weeks", 1),
    ("Box step-up", 2.0, True, "Drive through the full foot on the box", 1),
    ("Front lunge", 2.0, True, "Knee tracks over the toes", 1),
    ("Goblet squat", 3.0, False, "Weight at the chest, upright torso, controlled", 4),
    ("2-hand kettlebell swing", 2.0, False, "Hinge from the hips, not a squat", 4),
]
COOLDOWN_S = 10 * 60

CORE_WARM = [("Cat-cow", ""), ("Glute bridge", ""), ("Dead bug, slow", ""), ("Bird dog, slow, alternating", "")]
CORE_EX = [
    ("Dead bug", "Lower back stays on the floor, 3 s per reach", "Anti-extension"),
    ("Side plank right", "Raise the top leg once it gets easy", "Lateral hip, downhill stability"),
    ("Side plank left", "Same as right", "Lateral hip, downhill stability"),
    ("Single-leg glute bridge", "Switch sides every 5 reps", "Glutes, hip extension for climbing"),
    ("Plank with shoulder taps", "Hips still, feet wide", "Anti-rotation"),
]


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _step(t: str, label: str, dur: float | None, **kw) -> dict:
    s = {"t": t, "label": label, "dur": dur}
    s.update({k: v for k, v in kw.items() if v not in (None, "")})
    if t == "work":
        s["img"] = key(label.split(" · ")[0])
    return s


def me_workout(wo: int, sets: int | None = None) -> dict:
    if not isinstance(wo, int) or isinstance(wo, bool) or wo not in ME_WO:
        raise ValueError("wo must be a whole number 1-14")
    d_sets, load, rs, ri, re_, note = ME_WO[wo]
    sets = d_sets if sets is None else sets
    if not isinstance(sets, int) or isinstance(sets, bool) or not 4 <= sets <= 8:
        raise ValueError("sets must be a whole number 4-8")
    steps = []   # the warm-up is done by hand, not part of the session
    ex = [e for e in ME_EX if wo >= e[4]]
    for ei, (name, tempo, uni, cue, _) in enumerate(ex):
        for s in range(1, sets + 1):
            if s == 1:
                steps.append(_step("rest", f"Next: {name}", re_ if ei else 10, cue=cue))
            else:
                steps.append(_step("rest", f"Rest · {name} set {s}/{sets}", rs))
            for si, side in enumerate(("right", "left") if uni else (None,)):
                if si:
                    steps.append(_step("rest", f"Switch side · {name}", ri))
                steps.append(_step("work", f"{name}" + (f" · {side.upper()}" if side else ""), ME_REPS * tempo,
                                   cue=cue, reps=ME_REPS, tempo=tempo, set=f"{s}/{sets}", load=load))
    steps.append(_step("cool", "Easy cool-down", COOLDOWN_S, cue="Bring the heart rate down"))
    return {"kind": "me", "name": f"ME WO {wo}", "wo": wo, "sets": sets, "load": load, "note": note,
            "steps": steps, "duration_s": sum(s["dur"] or 90 for s in steps)}


def core15_workout(rounds: int = 3, hard: bool = False) -> dict:
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 2 <= rounds <= 4:
        raise ValueError("rounds must be a whole number 2-4")
    work, change = (45, 5) if hard else (40, 10)
    steps = [_step("warm", f"Warm-up · {n}", 30, cue=c) for n, c in CORE_WARM]
    steps[0]["cue"] = "2 min warm-up, 30 s each"
    for r in range(1, rounds + 1):
        for i, (n, cue, focus) in enumerate(CORE_EX):
            steps.append(_step("work", n, work, cue=cue, focus=focus, set=f"{r}/{rounds}"))
            if not (r == rounds and i == len(CORE_EX) - 1):
                steps.append(_step("rest", "Change" if i < len(CORE_EX) - 1 else f"Round {r + 1} next", change))
    return {"kind": "core15", "name": "Core 15", "rounds": rounds, "hard": hard,
            "note": "Only a mat. Best straight after an easy Z1 run.", "steps": steps,
            "duration_s": sum(s["dur"] or 0 for s in steps)}


def build(kind: str, **kw) -> dict:
    if kind == "me":
        return me_workout(kw.get("wo"), kw.get("sets"))
    if kind == "core15":
        return core15_workout(kw.get("rounds") or 3, bool(kw.get("hard")))
    raise ValueError("kind must be me or core15")


# ---- calendar marker --------------------------------------------------------------------
MARKER = re.compile(r"^\s*KiekeRun:\s*(me|core15)\b(.*)$", re.I | re.M)


def marker(kind: str, **kw) -> str:
    parts = [kind]
    for k in ("wo", "sets", "rounds"):
        if kw.get(k) is not None:
            parts.append(f"{k}={kw[k]}")
    if kw.get("hard"):
        parts.append("hard=1")
    return "KiekeRun: " + " ".join(parts)


def parse_marker(desc: str | None) -> tuple[str, dict] | None:
    m = MARKER.search(desc or "")
    if not m:
        return None
    kw: dict = {}
    for k, v in re.findall(r"(\w+)=(\d+)", m.group(2)):
        kw[k] = int(v)
    if kw.get("hard"):
        kw["hard"] = True
    return m.group(1).lower(), kw


def title(kind: str, **kw) -> str:
    return f"ME WO {kw['wo']}" if kind == "me" else "Core 15"


def overview(w: dict) -> str:
    """Readable calendar text below the marker line."""
    if w["kind"] == "me":
        names = []
        for s in w["steps"]:
            if s["t"] == "work":
                n = s["label"].split(" · ")[0]
                if n not in names:
                    names.append(n)
        lines = [f"Muscular endurance WO {w['wo']}: {w['sets']} x {ME_REPS} per exercise, {w['load']}"]
        lines += ["- " + n for n in names]
        if w["note"]:
            lines.append(w["note"])
    else:
        lines = [f"Core 15: {w['rounds']} rounds, " + ("45 s work / 5 s change" if w["hard"] else "40 s work / 10 s change")]
        lines += ["- " + n for n, _, _ in CORE_EX]
    return "\n".join(lines)


def progress(cl, today: str) -> dict:
    """Last done and already planned ME WO / Core sessions, found by the marker in activities and calendar."""
    t = datetime.strptime(today, "%Y-%m-%d").date()
    out = {"me": {"last_done": None, "planned": []}, "core15": {"last_done": None, "planned": []}}
    acts = cl.list_activities((t - timedelta(days=200)).isoformat(), today, fields="id,name,description,start_date_local")
    for a in sorted(acts, key=lambda x: str(x.get("start_date_local"))):
        mk = parse_marker(a.get("description"))
        if mk:
            out[mk[0]]["last_done"] = {"date": str(a.get("start_date_local"))[:10], **mk[1]}
    for e in cl.list_events(today, (t + timedelta(days=120)).isoformat()):
        mk = parse_marker(e.get("description"))
        if mk:
            out[mk[0]]["planned"].append({"date": str(e.get("start_date_local"))[:10], "event_id": e.get("id"), **mk[1]})
    ld = out["me"]["last_done"]
    nxt = (ld.get("wo") or 0) + 1 if ld else 1
    if out["me"]["planned"]:
        nxt = max(nxt, max(p.get("wo", 0) for p in out["me"]["planned"]) + 1)
    out["me"]["next_wo"] = min(14, nxt)
    return out
