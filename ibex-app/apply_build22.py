#!/usr/bin/env python3
"""BUILD 22 -- register feature_recency + feature_markov in feature_functions.py.

Run ONCE from the repo root:
    py -3.13 apply_build22.py

Inserts the two import lines immediately after the feature_temporal import
(import side-effect is what registers features in REGISTRY, which is what
FeaturePipeline builds and retrain_v2.py trains on).

Idempotent: safe to run twice. Backs up feature_functions.py to
feature_functions.py.build22.bak. Compiles all touched files. Dies loudly if
an anchor or a required file is missing -- it never half-patches.
DELETE THIS FILE after a successful run.
"""
import os
import py_compile
import shutil
import sys

FF = os.path.join("obcredit", "feature_functions.py")
REC = os.path.join("obcredit", "feature_recency.py")
MK = os.path.join("obcredit", "feature_markov.py")

ANCHOR = "from . import feature_temporal as _feature_temporal"
INS_REC = ("from . import feature_recency as _feature_recency  "
           "# noqa: E402,F401  # BUILD 22 RECENCY")
INS_MK = ("from . import feature_markov as _feature_markov    "
          "# noqa: E402,F401  # BUILD 22 MARKOV")


def die(msg: str) -> None:
    print("ERROR: " + msg)
    sys.exit(1)


def main() -> None:
    for p in (FF, REC, MK):
        if not os.path.exists(p):
            die(f"{p} not found. Copy feature_recency.py AND feature_markov.py "
                f"into obcredit\\ first, and run this from the repo root.")

    src = open(FF, encoding="utf-8").read()
    lines = src.splitlines()

    def find(needle: str):
        for i, line in enumerate(lines):
            if needle in line:
                return i
        return None

    i_anchor = find(ANCHOR)
    i_rec = find("BUILD 22 RECENCY")
    i_mk = find("BUILD 22 MARKOV")

    if i_anchor is None and i_rec is None:
        die("anchor import for feature_temporal not found in "
            "feature_functions.py -- file layout changed; STOP.")

    changes = []
    if i_rec is None:
        lines.insert(i_anchor + 1, INS_REC)
        i_rec = i_anchor + 1
        changes.append("registered feature_recency (11 recency features)")
    if i_mk is None:
        lines.insert(i_rec + 1, INS_MK)
        changes.append("registered feature_markov (6 transition features)")

    if not changes:
        print("BUILD 22 already applied -- nothing to do.")
    else:
        shutil.copyfile(FF, FF + ".build22.bak")
        trailing = "\n" if src.endswith("\n") else ""
        open(FF, "w", encoding="utf-8").write("\n".join(lines) + trailing)
        for c in changes:
            print("  +", c)

    for p in (FF, REC, MK):
        py_compile.compile(p, doraise=True)
        print("  compiled OK:", p)

    print("BUILD 22 patch complete. Backup: obcredit/feature_functions.py.build22.bak")
    print("Next: smoke build into a FRESH cache dir (do NOT reuse old shards).")


if __name__ == "__main__":
    main()
