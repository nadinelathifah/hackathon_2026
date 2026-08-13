#!/usr/bin/env python3
"""Build the OB feature matrix over the FULL Home Credit population, in shards.

Why this exists
---------------
The BUILD 18 calibration ran on 305,332 rows. That is not the whole population:
it is however many applicants survived a single in-memory build before you ran
out of patience or RAM. The consequence is the tail problem -- only 175 people
in the top zero-default block, so the score ceiling collapsed to ~672 and 36%
of the tail PD estimate came from the prior rather than from data.

This script builds the same matrix using the SAME f() (identical adapters,
identical FeaturePipeline, identical REGISTRY features), but:

  * writes SHARDS to disk as it goes, so RAM never holds the whole population;
  * records a manifest, so --resume picks up exactly where it stopped;
  * merges the shards into one pickle that calibrate_score.py --reuse-ob eats.

Nothing about the feature definitions changes. This is purely a throughput and
memory fix, so ob -> ob remains a like-for-like comparison against BUILD 18.

Run order
---------
  1) python scripts/build_ob_full.py <kaggle_dir> --cache-dir <cache> --resume
  2) python scripts/build_ob_full.py <kaggle_dir> --cache-dir <cache> --merge-only
  3) python scripts/calibrate_score.py <kaggle_dir> --max-cases 0 \\
         --cache-dir <cache> --reuse-ob <cache>\\ob_matrix_full_all.pkl

Step 1 is interruptible. Ctrl-C, reboot, run it again with --resume.
"""
from __future__ import annotations
import argparse
import itertools
import json
import os
import pickle
import sys
import time
from multiprocessing import Pool

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
_LGBM2 = os.path.join(_ROOT, "lgbm_2")
for _p in (_ROOT, _SCRIPTS, _LGBM2):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter  # noqa: E402
from obcredit.feature_registry import REGISTRY                 # noqa: E402
from obcredit.pipeline import FeaturePipeline                  # noqa: E402
from step3lib.kaggle_stream import canonical_pop_to_ground_truth  # noqa: E402
from step3lib.renderers import to_truelayer_payloads           # noqa: E402
from run_step3 import read_labels                              # noqa: E402

SHARD_DIR = "ob_shards"
MANIFEST = "ob_shards_manifest.json"
DEFAULT_OUT = "ob_matrix_full_all.pkl"
TARGET = "target"
WEEKCOL = "__week__"

_PIPE = None


def _init_worker():
    global _PIPE
    _PIPE = FeaturePipeline()


def _work_chunk(chunk):
    """Canonical applicants -> OB-reconstructed feature rows.

    This is byte-for-byte the same path run_compare.py uses for the OB side:
    canonical -> ground truth -> TrueLayer payloads -> TrueLayerAdapter ->
    canonical -> FeaturePipeline. Do not shortcut it; the whole point of ob->ob
    is that training inputs are produced by the same reconstruction that the
    live TrueLayer endpoint uses.
    """
    gt = canonical_pop_to_ground_truth(chunk)
    return _PIPE.build_matrix(TrueLayerAdapter(to_truelayer_payloads(gt)).to_canonical())


def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        block = list(itertools.islice(it, size))
        if not block:
            return
        yield block


def _build_stamp() -> str:
    try:
        with open(os.path.join(_ROOT, "VERSION.txt"), "r", encoding="utf-8") as vf:
            return vf.readline().strip()
    except OSError:
        return "BUILD ??? (VERSION.txt not found)"


def _manifest_path(cache_dir):
    return os.path.join(cache_dir, MANIFEST)


def _load_manifest(cache_dir, stamp):
    p = _manifest_path(cache_dir)
    if not os.path.exists(p):
        return {"build": stamp, "shards": []}
    with open(p, "r", encoding="utf-8") as f:
        man = json.load(f)
    if man.get("build") != stamp:
        raise SystemExit(
            f"\nREFUSING TO CONTINUE.\n"
            f"  existing shards were built under : {man.get('build')}\n"
            f"  current code reports             : {stamp}\n"
            f"Mixing feature definitions across builds silently corrupts the\n"
            f"matrix. Either check out the matching build, or delete\n"
            f"  {os.path.join(cache_dir, SHARD_DIR)}\n  {p}\nand start again.")
    return man


def _save_manifest(cache_dir, man):
    tmp = _manifest_path(cache_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2)
    os.replace(tmp, _manifest_path(cache_dir))


def _done_ids(cache_dir, man):
    """case_ids already written to a shard, read from the .ids.txt sidecars."""
    done = set()
    sd = os.path.join(cache_dir, SHARD_DIR)
    for sh in man.get("shards", []):
        ids_path = os.path.join(sd, sh["file"].replace(".pkl", ".ids.txt"))
        if os.path.exists(ids_path):
            with open(ids_path, "r", encoding="utf-8") as f:
                done.update(line.strip() for line in f if line.strip())
    return done


def _write_shard(cache_dir, man, frames, idx, float32=True):
    sd = os.path.join(cache_dir, SHARD_DIR)
    os.makedirs(sd, exist_ok=True)
    df = pd.concat(frames)
    df.index = [str(i) for i in df.index]
    if float32:
        for c in df.columns:
            if str(df[c].dtype) == "float64":
                df[c] = df[c].astype("float32")
    name = f"ob_shard_{idx:05d}.pkl"
    with open(os.path.join(sd, name), "wb") as f:
        pickle.dump(df, f, protocol=4)
    with open(os.path.join(sd, name.replace(".pkl", ".ids.txt")), "w",
              encoding="utf-8") as f:
        f.write("\n".join(df.index))
    mb = os.path.getsize(os.path.join(sd, name)) / 1e6
    man["shards"].append({"file": name, "rows": int(len(df)), "mb": round(mb, 1),
                          "written_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save_manifest(cache_dir, man)
    print(f"      [shard] wrote {name}  {len(df):,} rows  {mb:0.1f} MB", flush=True)
    return df.shape[1]


def build(args) -> int:
    stamp = _build_stamp()
    print(stamp)
    os.makedirs(args.cache_dir, exist_ok=True)
    man = _load_manifest(args.cache_dir, stamp)

    done = _done_ids(args.cache_dir, man) if args.resume else set()
    if args.resume:
        print(f"      [resume] {len(done):,} case_ids already sharded "
              f"across {len(man['shards'])} shards")
    elif man["shards"]:
        raise SystemExit(
            f"{len(man['shards'])} shards already exist. Pass --resume to continue "
            f"them, or delete {os.path.join(args.cache_dir, SHARD_DIR)} and the "
            f"manifest to start clean.")

    mc = None if not args.max_cases else args.max_cases
    adapter = KaggleAdapter.from_parquet_dir(args.kaggle_dir, max_cases=mc)

    def _stream():
        for a in adapter.stream_canonical():
            if str(a.case_id) not in done:
                yield a

    tasks = _chunks(_stream(), args.batch)
    frames, buffered, n_done, n_cols = [], 0, 0, 0
    shard_idx = len(man["shards"])
    t0 = time.time()

    def _flush():
        nonlocal frames, buffered, shard_idx, n_cols
        if not frames:
            return
        n_cols = _write_shard(args.cache_dir, man, frames, shard_idx, args.float32)
        shard_idx += 1
        frames, buffered = [], 0

    def _consume(res_iter):
        nonlocal frames, buffered, n_done
        for df in res_iter:
            if df is None or df.empty:
                continue
            frames.append(df)
            buffered += len(df)
            n_done += len(df)
            rate = n_done / max(1e-9, time.time() - t0)
            print(f"      built {n_done:,} applicants  ({rate:0.0f}/s)", flush=True)
            if buffered >= args.shard_size:
                _flush()

    try:
        if args.workers <= 1:
            _init_worker()
            _consume(map(_work_chunk, tasks))
        else:
            with Pool(processes=args.workers, initializer=_init_worker) as pool:
                _consume(pool.imap_unordered(_work_chunk, tasks))
    except KeyboardInterrupt:
        print("\n      [interrupt] flushing what is buffered ...")
    finally:
        _flush()

    total = sum(s["rows"] for s in man["shards"])
    print(f"\n      [build] {len(man['shards'])} shards, {total:,} rows, "
          f"{n_cols} feature columns")
    print("      next:  --merge-only")
    return 0


def merge(args) -> int:
    stamp = _build_stamp()
    print(stamp)
    man = _load_manifest(args.cache_dir, stamp)
    if not man["shards"]:
        raise SystemExit("no shards to merge -- run the build step first.")
    sd = os.path.join(args.cache_dir, SHARD_DIR)

    parts = []
    for sh in man["shards"]:
        with open(os.path.join(sd, sh["file"]), "rb") as f:
            parts.append(pickle.load(f))
        print(f"      [merge] {sh['file']}  {sh['rows']:,} rows", flush=True)
    m = pd.concat(parts)
    del parts
    before = len(m)
    m = m[~m.index.duplicated(keep="first")]
    if len(m) != before:
        print(f"      [merge] dropped {before - len(m):,} duplicate case_ids")

    print("      [merge] attaching labels (target + competition week) ...")
    mc = None if not args.max_cases else args.max_cases
    labels = read_labels(args.kaggle_dir, max_cases=mc)
    m.index = [str(i) for i in m.index]
    ids = list(m.index)
    m[TARGET] = [labels.get(i, (None, None))[0] for i in ids]
    m[WEEKCOL] = [labels.get(i, (None, None))[1] for i in ids]

    labelled = int(m[TARGET].notna().sum())
    out = args.merge_out or os.path.join(args.cache_dir, DEFAULT_OUT)
    with open(out, "wb") as f:
        pickle.dump(m, f, protocol=4)
    meta = out.replace(".pkl", ".meta.json")
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"build": stamp, "features": list(REGISTRY.parity_names()),
                   "max_cases": 0, "rows": int(len(m)), "labelled": labelled,
                   "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)

    print(f"\n      [merge] wrote {out}")
    print(f"      rows            : {len(m):,}")
    print(f"      LABELLED rows   : {labelled:,}   <-- this is what trains")
    print(f"      unlabelled      : {len(m) - labelled:,}")
    if labelled < 400000:
        print("\n      WARNING: fewer labelled rows than expected. Labels come")
        print("      only from base*.parquet. If that file is missing from the")
        print("      kaggle_dir the adapter silently falls back to bureau")
        print("      case_ids and most rows end up unlabelled -- that is a data")
        print("      download problem, not a code problem.")
    print("\n      next:  calibrate_score.py ... --reuse-ob \"%s\"" % out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Shard-build the full OB matrix.")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--shard-size", type=int, default=150000)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--workers", type=int,
                    default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--merge-only", action="store_true")
    ap.add_argument("--merge-out", default=None)
    ap.add_argument("--float32", dest="float32", action="store_true", default=True)
    ap.add_argument("--no-float32", dest="float32", action="store_false")
    args = ap.parse_args()
    return merge(args) if args.merge_only else build(args)


if __name__ == "__main__":
    raise SystemExit(main())
