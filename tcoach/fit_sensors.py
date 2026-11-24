"""Where did the heart rate of an activity come from? Chest strap / external sensor or the watch's wrist sensor.

Reads only the device_info messages (global message 23) of a FIT file and skips everything else by size,
so a 20-hour race file is read in well under a second (fit-tool decodes every record and needs ~40 s).

FIT profile values used:
  device_info fields: 0 device_index, 1 device_type, 2 manufacturer, 4 product, 25 source_type, 27 product_name
  source_type: 0 ant, 1 antplus, 2 bluetooth, 3 bluetooth_low_energy, 4 wifi, 5 local
  device_type: antplus/ant 120 = heart_rate · ble 1 = heart_rate · local 10 = wrist heart rate (whr)
"""
from __future__ import annotations

import gzip
import io
import struct
import zipfile

DEVICE_INFO = 23
_BASE_SIZE = {0: 1, 1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4, 7: 1, 8: 4, 9: 8, 10: 1, 11: 2, 12: 4, 13: 1, 14: 8, 15: 8, 16: 8}
SOURCE_NAMES = {0: "ant", 1: "antplus", 2: "bluetooth", 3: "bluetooth_low_energy", 4: "wifi", 5: "local"}
GARMIN = 1
GARMIN_HR_PRODUCTS = {  # garmin_product ids of Garmin HR straps / arm bands
    1: "HRM1", 1743: "HRM-Tri", 1752: "HRM-Run", 2327: "HRM4-Run", 3299: "HRM-Dual", 3300: "HRM-Pro",
    4024: "HRM-Pro Plus", 4339: "HRM-Fit", 4352: "HRM 200", 4369: "HRM 600"}


def unpack(data: bytes) -> bytes:
    """intervals.icu serves original files as they were uploaded, sometimes gzipped or zipped."""
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".fit")), z.namelist()[0])
            return z.read(name)
    return data


def device_infos(data: bytes) -> list[dict]:
    data = unpack(data)
    if len(data) < 12 or data[8:12] != b".FIT":
        raise ValueError("not a FIT file")
    hsize = data[0]
    end = hsize + struct.unpack("<I", data[4:8])[0]
    pos, defs, out = hsize, {}, []
    while pos < min(end, len(data)):
        h = data[pos]
        pos += 1
        if h & 0x80:  # compressed timestamp header: data message of local type (h>>5)&3
            d = defs.get((h >> 5) & 0x03)
            if d is None:
                raise ValueError("data before definition")
            pos += d["size"]
            continue
        local = h & 0x0F
        if h & 0x40:  # definition message
            big = data[pos + 1] == 1
            gnum = struct.unpack(">H" if big else "<H", data[pos + 2:pos + 4])[0]
            nf = data[pos + 4]
            p = pos + 5
            fields = [(data[p + 3 * i], data[p + 3 * i + 1], data[p + 3 * i + 2]) for i in range(nf)]
            p += 3 * nf
            dev_size = 0
            if h & 0x20:  # developer fields
                nd = data[p]
                dev_size = sum(data[p + 1 + 3 * i + 1] for i in range(nd))
                p += 1 + 3 * nd
            defs[local] = {"g": gnum, "big": big, "fields": fields,
                           "size": sum(f[1] for f in fields) + dev_size}
            pos = p
            continue
        d = defs.get(local)
        if d is None:
            raise ValueError("data before definition")
        if d["g"] == DEVICE_INFO:
            out.append(_decode(data, pos, d))
        pos += d["size"]
    return out


def _decode(data: bytes, pos: int, d: dict) -> dict:
    rec, p = {}, pos
    e = ">" if d["big"] else "<"
    for num, size, btype in d["fields"]:
        raw = data[p:p + size]
        p += size
        base = btype & 0x1F
        if base == 7:  # string
            rec[num] = raw.split(b"\x00", 1)[0].decode("utf-8", "replace") or None
            continue
        fmt = {1: "B", 2: "B", 0: "B", 10: "B", 13: "B", 3: "h", 4: "H", 11: "H", 5: "i", 6: "I", 12: "I"}.get(base)
        if fmt is None or size != struct.calcsize(fmt):
            continue
        v = struct.unpack(e + fmt, raw)[0]
        invalid = {"B": 0xFF, "h": 0x7FFF, "H": 0xFFFF, "i": 0x7FFFFFFF, "I": 0xFFFFFFFF}[fmt]
        if base == 10 or base == 0:
            invalid = 0 if base == 10 else 0xFF
        if v != invalid:
            rec[num] = v
    return rec


def hr_source(infos: list[dict]) -> dict:
    """chest = external heart-rate sensor connected (ANT+/BLE strap or arm band);
    wrist = only the watch's optical sensor; unknown = the file says neither."""
    ext, wrist = [], False
    for r in infos:
        src, dtype = r.get(25), r.get(1)
        if src in (0, 1) and dtype == 120 or src in (2, 3) and dtype == 1:
            name = r.get(27) or (GARMIN_HR_PRODUCTS.get(r.get(4)) if r.get(2) == GARMIN else None)
            ext.append({"via": SOURCE_NAMES.get(src), "name": name})
        elif src == 5 and dtype == 10:
            wrist = True
    if ext:
        uniq = {(e["via"], e["name"]) for e in ext}
        return {"hr_source": "chest", "hr_sensors": [{"via": v, **({"name": n} if n else {})} for v, n in sorted(uniq, key=str)]}
    if wrist:
        return {"hr_source": "wrist"}
    return {"hr_source": "unknown"}
