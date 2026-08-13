#!/usr/bin/env python3
"""BUILD 22 -- reuse guard for retrain_v2.py.

Three times now, retrain_v2.py has been run with --reuse-ob pointing at a
matrix that does not exist yet, and instead of failing it has silently fallen
back to a FULL in-memory Kaggle rebuild -- which runs for several minutes and
then dies with ArrowMemoryError / 'paging file too small'.

This patch makes that failure loud and instant: if --reuse-ob is given but
the file is missing, the script stops immediately and tells you to run the
sharded builder (scripts/build_ob_full.py) first. No behavioural change when
the file exists.

Run ONCE from the repo root:
    py -3.13 apply_reuse_guard.py
Idempotent. Backs up to scripts/retrain_v2.py.reuseguard.bak. Dies loudly if
any anchor is missing. DELETE this file after a successful run.
"""
import os
import py_compile
import shutil
import sys

RT = os.path.join("scripts", "retrain_v2.py")
MARKER = "BUILD 22 REUSE GUARD"
NEEDLE = "rc.build_or_load_matrices("


def die(msg: str) -> None:
    print("ERROR: " + msg)
    sys.exit(1)


def main() -> None:
    if not os.path.exists(RT):
        die(f"{RT} not found -- run this from the repo root.")
    src = open(RT, encoding="utf-8").read()
    if MARKER in src:
        print("guard already installed -- nothing to do.")
        return
    if "import os" not in src:
        die("retrain_v2.py does not import os -- unexpected layout; STOP and tell me.")

    lines = src.splitlines()
    idx = next((i for i, l in enumerate(lines)
                if NEEDLE in l and "kg" in l and "=" in l), None)
    if idx is None:
        die("could not find the 'kg, ob = rc.build_or_load_matrices(' line -- "
            "file layout changed; STOP and tell me.")

    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    guard = [
        f"{indent}# {MARKER}: never fall back to an in-memory rebuild when a reuse",
        f"{indent}# file was requested -- that path holds the whole population in",
        f"{indent}# RAM and dies with ArrowMemoryError by design.",
        f"{indent}if args.reuse_ob and not os.path.exists(args.reuse_ob):",
        f'{indent}    raise SystemExit(',
        f'{indent}        "--reuse-ob file not found: " + str(args.reuse_ob) +',
        f'{indent}        "\\nRun the sharded builder first: '
        f'scripts\\\\build_ob_full.py <kaggle_dir> --cache-dir <cache> --resume, '
        f'then --merge-only.")',
    ]
    lines[idx:idx] = guard

    shutil.copyfile(RT, RT + ".reuseguard.bak")
    open(RT, "w", encoding="utf-8").write(
        "\n".join(lines) + ("\n" if src.endswith("\n") else ""))
    py_compile.compile(RT, doraise=True)
    print("guard installed above the build_or_load_matrices call; compiled OK.")
    print("backup: scripts/retrain_v2.py.reuseguard.bak")
    print("DELETE apply_reuse_guard.py now -- it is a run-once patcher.")


if __name__ == "__main__":
    main()
