"""BUILD 19 -- fix the mislabelled ceiling counter in serve/ibex_v4.py.

The admin cohort reported "at_ceiling" using at_top, which is the count of
rows at THIS SAMPLE's maximum score, not the count at the policy ceiling
implied by the live PD floor. Both readings are numbers; only one is the
ceiling. This also refreshes four evidence literals left over from the
superseded 0.0075 floor.

Run once from the project root, then delete this file.
"""
import io
import os
import shutil
import sys

TARGET = os.path.join("serve", "ibex_v4.py")
BACKUP = TARGET + ".ceiling.bak"

HELPER = (
    "def _ceiling_now():\n"
    "    # Highest score the LIVE pd floor permits. Recomputed on every call,\n"
    "    # so it follows the calibrator instead of going stale.\n"
    "    f = _floor_now()\n"
    "    if not f or not (0.0 < f < 1.0):\n"
    "        return None\n"
    "    return round(OFFSET + FACTOR * math.log((1.0 - f) / f), 1)\n"
    "\n"
    "\n"
    "def _at_ceiling(rows):\n"
    "    # Rows sitting at the POLICY CEILING -- not merely at this sample's\n"
    "    # maximum score, which is what at_top counts.\n"
    "    c = _ceiling_now()\n"
    "    if c is None:\n"
    "        return 0\n"
    "    out = 0\n"
    "    for r in rows:\n"
    "        try:\n"
    "            v = float(r.get('score'))\n"
    "        except (AttributeError, TypeError, ValueError):\n"
    "            continue\n"
    "        if v >= c - 0.05:\n"
    "            out += 1\n"
    "    return out\n"
    "\n"
    "\n"
)

EDITS = [
    (
        '"at_ceiling": at_top,',
        '"at_top_score": at_top,\n'
        '            "ceiling_score": _ceiling_now(),\n'
        '            "at_ceiling": _at_ceiling(rows),',
    ),
    (
        '"pct_at_ceiling": round(100.0 * at_top / max(1, len(rows)), 1),',
        '"pct_at_top_score": round(100.0 * at_top / max(1, len(rows)), 1),\n'
        '            "pct_at_ceiling": round(\n'
        '                100.0 * _at_ceiling(rows) / max(1, len(rows)), 1),',
    ),
    ('"score": 709, "pd": 0.01135', '"score": 759.5, "pd": 0.00314465'),
    (
        '"pd_ci": [0.00750, 0.01376], "score_ci": [673.6, 709.0]',
        '"pd_ci": [0.00031, 0.00977], "score_ci": [693.7, 759.5]',
    ),
    ('"ceiling_range": [680.5, 743.0]', '"ceiling_range": [693.7, 759.5]'),
    ('"floor_ci": [0.00418, 0.01223]', '"floor_ci": [0.00031, 0.00977]'),
]


def main():
    if not os.path.isfile(TARGET):
        print("cannot find " + TARGET)
        print("run this from the project root (the folder containing serve/)")
        return 1

    src = io.open(TARGET, encoding="utf-8").read()
    out = src
    log = []

    if "def _at_ceiling(" in out:
        log.append("helpers already present, skipped")
    else:
        i = out.find("def _floor_now(")
        if i < 0:
            print("could not find 'def _floor_now(' -- nothing written")
            return 2
        out = out[:i] + HELPER + out[i:]
        log.append("inserted _ceiling_now and _at_ceiling")

    for old, new in EDITS:
        n = out.count(old)
        if n == 1:
            out = out.replace(old, new)
            log.append("patched   " + old[:58])
        elif n == 0:
            log.append("skipped   " + old[:58])
        else:
            print("ambiguous match, " + str(n) + " copies of:")
            print("  " + old)
            print("nothing written")
            return 3

    if out == src:
        print("nothing to change, file already patched")
        return 0

    if not os.path.exists(BACKUP):
        shutil.copyfile(TARGET, BACKUP)
        log.append("backup    " + BACKUP)

    io.open(TARGET, "w", encoding="utf-8", newline="").write(out)

    for line in log:
        print("  " + line)
    print("")
    print("done. now run:  py -3.13 -m py_compile serve/ibex_v4.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
