"""BUILD 21 wiring: register the Markov features + add a policy --drop to retrain_v2.

Run from the repo root. Idempotent: safe to run twice.
Backs up every file it touches, py_compiles them, and prints an exact change log.
"""
import os
import py_compile
import shutil
import sys

FF = os.path.join("obcredit", "feature_functions.py")
RT = os.path.join("scripts", "retrain_v2.py")
MK = os.path.join("obcredit", "feature_markov.py")

changes = []


def die(msg):
    print("STOP: " + msg)
    sys.exit(1)


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read().splitlines(keepends=True)


def write(p, lines):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))


def find_line(lines, needle, what):
    hits = [i for i, ln in enumerate(lines) if needle in ln]
    if not hits:
        die("could not find %s (looked for %r). File not patched." % (what, needle))
    return hits[0]


def indent_of(line):
    return line[: len(line) - len(line.lstrip())]


# --------------------------------------------------------------------- #
# 0. sanity
# --------------------------------------------------------------------- #
for p in (FF, RT):
    if not os.path.isfile(p):
        die("%s not found -- run this from the repo root (the folder with obcredit/ and scripts/)." % p)
if not os.path.isfile(MK):
    die("%s not found -- copy feature_markov.py into obcredit/ first." % MK)

# --------------------------------------------------------------------- #
# 1. register feature_markov by importing it in feature_functions.py
# --------------------------------------------------------------------- #
lines = read(FF)
if any("feature_markov" in ln for ln in lines):
    changes.append("feature_functions.py : already imports feature_markov (skipped)")
else:
    i = find_line(lines, "import feature_temporal", "the feature_temporal import")
    new = "from . import feature_markov as _feature_markov  # noqa: E402,F401  BUILD 21 MARKOV\n"
    shutil.copyfile(FF, FF + ".markov.bak")
    lines.insert(i + 1, new)
    write(FF, lines)
    changes.append("feature_functions.py : +1 line after the feature_temporal import (line %d)" % (i + 2))

# --------------------------------------------------------------------- #
# 2. add --drop to retrain_v2.py and apply it before any gate
# --------------------------------------------------------------------- #
lines = read(RT)
if any("BUILD 21 POLICY DROP" in ln for ln in lines):
    changes.append("retrain_v2.py        : already has --drop (skipped)")
else:
    shutil.copyfile(RT, RT + ".drop.bak")

    # 2a. the argparse flag, inserted just before --force-keep
    i = find_line(lines, '"--force-keep"', "the --force-keep argparse line")
    ind = indent_of(lines[i])
    arg = [
        ind + 'ap.add_argument("--drop", default="declared_education_code",  # BUILD 21 POLICY DROP\n',
        ind + '                help="comma-separated features excluded on POLICY grounds before "\n',
        ind + '                     "any statistical gate (protected-characteristic proxies). "\n',
        ind + '                     "Pass an empty string to disable.")\n',
    ]
    lines[i:i] = arg

    # 2b. apply it right after the post-flag feature count print
    j = find_line(lines, "feature count:", "the post-flag feature-count print")
    ind = indent_of(lines[j])
    block = [
        "\n",
        ind + "# ---- BUILD 21 POLICY DROP: excluded before any statistical gate ----\n",
        ind + '_policy_drop = [c.strip() for c in (args.drop or "").split(",") if c.strip()]\n',
        ind + "_dropped_policy = [c for c in _policy_drop if c in cols_all]\n",
        ind + "if _dropped_policy:\n",
        ind + "    cols_all = [c for c in cols_all if c not in set(_dropped_policy)]\n",
        ind + "    for _c in _dropped_policy:\n",
        ind + "        monotone.pop(_c, None)\n",
        ind + '    print("      POLICY DROP (never enters a gate): " + ", ".join(_dropped_policy))\n',
        ind + '    print("      feature count after policy drop: %d" % len(cols_all))\n',
        ind + "_missing_policy = [c for c in _policy_drop if c not in _dropped_policy]\n",
        ind + "if _missing_policy:\n",
        ind + '    print("      NOTE: --drop named features that are not present: " + ", ".join(_missing_policy))\n',
        "\n",
    ]
    lines[j + 1: j + 1] = block
    write(RT, lines)
    changes.append("retrain_v2.py        : +4 argparse lines before --force-keep")
    changes.append("retrain_v2.py        : +13 lines applying the drop after the feature-count print")

# --------------------------------------------------------------------- #
# 3. compile-check everything we touched
# --------------------------------------------------------------------- #
for p in (FF, RT, MK):
    try:
        py_compile.compile(p, doraise=True)
    except py_compile.PyCompileError as exc:
        die("%s does not compile after patching:\n%s\nRestore from the .bak and tell me." % (p, exc))

print("BUILD 21 wiring applied\n")
for c in changes:
    print("  " + c)
print("\nall three files compile.")
print("backups: %s.markov.bak  %s.drop.bak" % (FF, RT))
