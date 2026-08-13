"""Kaggle Home Credit raw tables -> CanonicalApplicant.

The competition data is relational and keyed by case_id, with depth-1 tables
keyed by (case_id, num_group1) and depth-2 by (case_id, num_group1, num_group2).

DELINQUENCY = DAYS-PAST-DUE, READ DIRECTLY FROM THE BUREAU
----------------------------------------------------------
The depth-2 bureau payment tables record one row per scheduled payment and give
us the delinquency EXPLICITLY, in two columns we read directly (we do NOT infer
lateness from the coarse month-stamped dates -- on a monthly grid every payment
looks on-time, which is exactly why earlier date-inferred DPD was dead):

  credit_bureau_a_2 : DPD = pmts_dpd_1073P      overdue amt = pmts_overdue_1140A
                      timing = pmts_year_1139T + pmts_month_158T (NO date column)
  credit_bureau_b_2 : DPD = pmts_dpdvalue_108P  overdue amt = pmts_pmtsoverdue_635A
                      timing = pmts_date_1107D

(The *_303P / *_1152A variants are ~100% null in the real download and ignored.)
DPD is floored at 0 and CAPPED at cfg.dpd_clip_days -- both to tame the bureau's
absurd b_2 outliers (max ~1.8e8, not days) and to match the open-banking side,
where a reconstructed DPD is likewise capped. This is the parity-safe primitive:
  * Kaggle       : bureau-reported DPD, read here.
  * Open banking : DPD reconstructed from transaction timing by the schedule
                   model (see TrueLayerAdapter._assign_dpd).
Both feed CanonicalObligation.dpd_values(); every DPD feature is then identical.

Affordability uses the applprev monthly instalment annuity_853A (NOT annuity_780A,
which is not on this table -- reading the wrong name is why affordability was 0).

Memory-safe loading (the full competition data is ~80M+ rows)
-------------------------------------------------------------
  * Load ONLY the tables the engine uses, and ONLY the columns each needs.
  * With a row limit (smoke test) read `base` first, take the first N case_ids,
    and PUSH A FILTER DOWN into every other parquet read.
  * Files are chunked (`train_credit_bureau_a_2_7.parquet`); we strip the
    train_/test_ prefix, map to the logical table, and concat all chunks.
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

import pandas as pd

from ..canonical import (CanonicalAccount, CanonicalApplicant,
                         CanonicalObligation, CanonicalPayment)
from ..config import DEFAULT, EngineConfig
from ..logging_utils import get_logger
from .base import SourceAdapter

log = get_logger("kaggle_adapter")


def _to_date(v) -> Optional[date]:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return None


def _ym_to_date(year, month) -> Optional[date]:
    """Reconstruct a date from bureau year + month columns (day defaults to 1)."""
    try:
        if year is None or month is None or pd.isna(year) or pd.isna(month):
            return None
        y, m = int(year), int(month)
        if not (1 <= m <= 12) or y < 1900 or y > 2100:
            return None
        return date(y, m, 1)
    except (TypeError, ValueError):
        return None


def _num_or_zero(v) -> float:
    """Non-negative float; None/NaN/garbage -> 0.0."""
    if v is None:
        return 0.0
    try:
        if pd.isna(v):
            return 0.0
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def _clip_dpd(v, cap: float) -> float:
    """Days-past-due floored at 0 and capped (robust + parity with open banking)."""
    return min(_num_or_zero(v), cap)


def _first_present(cols, candidates: Sequence[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def _clean_str(v) -> Optional[str]:
    """Trimmed non-empty string, else None (NaN/None-safe)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


def _declared_dict(stated_income, income_type, education, housing,
                   employment) -> Dict[str, object]:
    """Assemble the declared-attribute dict, dropping missing values. Values pass
    through VERBATIM so the same dict can be rendered to both sources -> parity."""
    d: Dict[str, object] = {}
    try:
        if stated_income is not None and not pd.isna(stated_income) and float(stated_income) > 0:
            d["stated_income"] = float(stated_income)
    except (TypeError, ValueError):
        pass
    for key, val in (("income_type", income_type), ("education", education),
                     ("housing", housing), ("employment", employment)):
        cv = _clean_str(val)
        if cv is not None:
            d[key] = cv
    return d


# Logical Home Credit table names, longest-first so the most specific wins.
KNOWN_STEMS = [
    "base",
    "static_cb_0", "static_0",
    "applprev_2", "applprev_1",
    "credit_bureau_a_2", "credit_bureau_a_1",
    "credit_bureau_b_2", "credit_bureau_b_1",
    "debitcard_1", "deposit_1",
    "person_2", "person_1",
    "tax_registry_a_1", "tax_registry_b_1", "tax_registry_c_1",
    "other_1",
]


def _logical_stem(file_stem: str) -> Optional[str]:
    """Map a raw file stem (e.g. 'train_credit_bureau_a_2_7') to its table name."""
    name = file_stem
    for pre in ("train_", "test_"):
        if name.startswith(pre):
            name = name[len(pre):]
    for stem in sorted(KNOWN_STEMS, key=len, reverse=True):
        if name == stem or name.startswith(stem + "_"):
            tail = name[len(stem):]
            if tail == "" or tail.lstrip("_").isdigit():
                return stem
    return None


class KaggleAdapter(SourceAdapter):
    """Build canonical applicants from in-memory pandas frames (keyed by logical
    table name) or from a directory of competition parquet files."""

    COL_CASE = "case_id"
    COL_G1 = "num_group1"
    COL_ANNUITY = "annuity_853A"           # applprev monthly instalment (NOT 780A)
    COL_OPENDATE = "creationdate_885D"
    COL_INCOME = "maininc_215A"
    COL_DECISION = "date_decision"
    COL_CARD_BAL = "last180dayaveragebalance_704A"
    # declared onboarding attributes (person_1, num_group1==0); present in BOTH
    # the application data and the open-banking onboarding form -> parity-safe.
    COL_STATED_INCOME = "mainoccupationinc_384A"
    COL_INCOME_TYPE = "incometype_1044T"
    COL_EDUCATION = "education_927M"
    COL_HOUSING = "housetype_905L"
    COL_EMPLOYMENT = "empl_employedtotal_800L"

    # bureau depth-2 delinquency columns, read DIRECTLY (auto-detected per table).
    DPD_COLS = ("pmts_dpd_1073P", "pmts_dpdvalue_108P")        # a_2, b_2
    OVERDUE_COLS = ("pmts_overdue_1140A", "pmts_pmtsoverdue_635A")
    DATE_COLS = ("pmts_date_1107D",)
    YEAR_COLS = ("pmts_year_1139T", "pmts_year_507T")
    MONTH_COLS = ("pmts_month_158T", "pmts_month_706T")
    BUREAU_STEMS = ("credit_bureau_a_2", "credit_bureau_b_2")

    _BUREAU_COLS = [COL_CASE, COL_G1,
                    "pmts_dpd_1073P", "pmts_dpdvalue_108P",
                    "pmts_overdue_1140A", "pmts_pmtsoverdue_635A",
                    "pmts_date_1107D",
                    "pmts_year_1139T", "pmts_year_507T",
                    "pmts_month_158T", "pmts_month_706T"]

    # ONLY these tables are loaded, and ONLY these columns from each.
    USE_COLUMNS = {
        "base": [COL_CASE, COL_DECISION],
        "credit_bureau_a_2": _BUREAU_COLS,
        "credit_bureau_b_2": _BUREAU_COLS,
        "applprev_1": [COL_CASE, COL_G1, COL_ANNUITY, COL_OPENDATE],
        "static_0": [COL_CASE, COL_INCOME],
        "static_cb_0": [COL_CASE, COL_INCOME],
        "debitcard_1": [COL_CASE, COL_CARD_BAL],
        "person_1": [COL_CASE, COL_G1, COL_STATED_INCOME, COL_INCOME_TYPE,
                     COL_EDUCATION, COL_HOUSING, COL_EMPLOYMENT],
    }

    def __init__(self, frames: Dict[str, pd.DataFrame], cfg: EngineConfig = DEFAULT):
        self.frames = frames
        self.cfg = cfg
        # Streaming state (set by from_parquet_dir for the big real download).
        # credit_bureau_a_2 is ~188M rows and is NEVER held whole -- it is read
        # one chunk file at a time. _a2_test_chunks lets tests inject in-memory
        # chunks to exercise the streaming path without parquet.
        self._a2_files: List[str] = []
        self._keep: Optional[list] = None
        self._a2_test_chunks: Optional[List[pd.DataFrame]] = None
        self._build_indexes()

    # ----------------------------------------------------------------- load
    @classmethod
    def from_parquet_dir(cls, path: str, max_cases: Optional[int] = None,
                         cfg: EngineConfig = DEFAULT) -> "KaggleAdapter":
        """Load competition parquet files frugally.

        max_cases: if set, only the first N case_ids (from base) are read from
        EVERY table via a pushed-down filter -> fast, low-memory smoke test.
        """
        import glob, os
        import pyarrow.parquet as pq

        files = sorted(glob.glob(os.path.join(path, "*.parquet")))
        if not files:
            log.warning("no .parquet files found in %s", path)

        by_stem: Dict[str, List[str]] = {}
        for fp in files:
            file_stem = os.path.splitext(os.path.basename(fp))[0]
            stem = _logical_stem(file_stem)
            if stem is None or stem not in cls.USE_COLUMNS:
                continue
            by_stem.setdefault(stem, []).append(fp)

        keep: Optional[list] = None
        if max_cases is not None and "base" in by_stem:
            parts = [pd.read_parquet(fp, columns=[cls.COL_CASE]) for fp in by_stem["base"]]
            base_ids = pd.concat(parts, ignore_index=True)[cls.COL_CASE]
            keep = [int(x) for x in base_ids.head(max_cases).tolist()]
            log.info("smoke test: restricting ALL tables to first %d case_ids", len(keep))

        def _read(fp: str, stem: str) -> Optional[pd.DataFrame]:
            names = pq.ParquetFile(fp).schema.names
            cols = [c for c in cls.USE_COLUMNS[stem] if c in names]
            # a bureau payment table is useless without SOME timing (a date, or
            # a year+month pair to reconstruct one).
            if stem in cls.BUREAU_STEMS:
                has_date = any(c in names for c in cls.DATE_COLS)
                has_ym = (any(c in names for c in cls.YEAR_COLS)
                          and any(c in names for c in cls.MONTH_COLS))
                if not (has_date or has_ym):
                    log.info("skipping %s (no timing column)", os.path.basename(fp))
                    return None
            filters = None
            if keep is not None and cls.COL_CASE in names:
                filters = [(cls.COL_CASE, "in", keep)]
            return pd.read_parquet(fp, columns=cols, filters=filters)

        # credit_bureau_a_2 is the monster table (~188M rows across ~11 chunks).
        # We NEVER concatenate it into RAM -- we record its chunk files and stream
        # them one at a time in build_matrix_streaming(). Every OTHER table is
        # small enough to load now (b_2 ~1.3M, applprev ~6.5M, static/base ~1.5M).
        a2_files: List[str] = []
        for fp in by_stem.get("credit_bureau_a_2", []):
            names = pq.ParquetFile(fp).schema.names
            has_date = any(c in names for c in cls.DATE_COLS)
            has_ym = (any(c in names for c in cls.YEAR_COLS)
                      and any(c in names for c in cls.MONTH_COLS))
            if has_date or has_ym:
                a2_files.append(fp)
            else:
                log.info("skipping %s (no timing column)", os.path.basename(fp))

        frames: Dict[str, pd.DataFrame] = {}
        for stem, parts in by_stem.items():
            if stem == "credit_bureau_a_2":
                continue  # streamed later, never held whole
            loaded = []
            for fp in parts:
                df = _read(fp, stem)
                if df is None:
                    continue
                loaded.append(df)
                log.info("loaded %s -> '%s' rows=%d cols=%d",
                         os.path.basename(fp), stem, len(df), df.shape[1])
            if not loaded:
                continue
            frames[stem] = (pd.concat(loaded, ignore_index=True)
                            if len(loaded) > 1 else loaded[0])
            if len(loaded) > 1:
                log.info("table '%s': merged %d chunks -> %d rows",
                         stem, len(loaded), len(frames[stem]))

        adapter = cls(frames, cfg)
        adapter._a2_files = a2_files
        adapter._keep = keep
        log.info("credit_bureau_a_2 will be STREAMED from %d chunk(s) "
                 "(never held in memory)", len(a2_files))
        return adapter

    # --------------------------------------------------- pre-built indexes
    def _payment_frames(self) -> List[pd.DataFrame]:
        return [self.frames[s] for s in self.BUREAU_STEMS if self.frames.get(s) is not None]

    def _build_indexes(self) -> None:
        """Group large frames by case_id ONCE so per-applicant lookups are fast."""
        # payments: case_id -> list of sub-DataFrames (no full-frame copy)
        self._pay_by_case: Dict[str, List[pd.DataFrame]] = {}
        for pf in self._payment_frames():
            key = pf[self.COL_CASE].astype(str)
            for cid, sub in pf.groupby(key, sort=False):
                self._pay_by_case.setdefault(cid, []).append(sub)

        # per-line instalments: case_id -> list of applprev annuities (affordability;
        # parity-safe with the open-banking per-stream instalment)
        self._instalments_by_case: Dict[str, List[float]] = {}
        ap = self.frames.get("applprev_1")
        if ap is not None and self.COL_ANNUITY in ap.columns:
            for cid, ann in zip(ap[self.COL_CASE].astype(str), ap[self.COL_ANNUITY]):
                if ann is not None and not pd.isna(ann):
                    self._instalments_by_case.setdefault(cid, []).append(float(ann))

        # income: case_id -> monthly income
        self._income_by_case: Dict[str, float] = {}
        for stem in ("static_0", "static_cb_0"):
            sf = self.frames.get(stem)
            if sf is None or self.COL_INCOME not in sf.columns:
                continue
            for cid, inc in zip(sf[self.COL_CASE].astype(str), sf[self.COL_INCOME]):
                if cid not in self._income_by_case and inc is not None and not pd.isna(inc):
                    self._income_by_case[cid] = float(inc)

        # balances: case_id -> aggregate balance value
        self._balance_by_case: Dict[str, float] = {}
        df = self.frames.get("debitcard_1")
        if df is not None and self.COL_CARD_BAL in df.columns:
            for cid, bal in zip(df[self.COL_CASE].astype(str), df[self.COL_CARD_BAL]):
                if cid not in self._balance_by_case and bal is not None and not pd.isna(bal):
                    self._balance_by_case[cid] = float(bal)

        # decision dates: case_id -> as_of
        self._decision: Dict[str, date] = {}
        base = self.frames.get("base")
        if base is not None and self.COL_DECISION in base.columns:
            for cid, dec in zip(base[self.COL_CASE].astype(str), base[self.COL_DECISION]):
                self._decision[cid] = _to_date(dec) or date.today()

        # declared onboarding attributes: case_id -> dict (person_1, applicant
        # row num_group1==0). Defensive: a missing/renamed column degrades to
        # None, never a crash.
        self._declared_by_case: Dict[str, dict] = {}
        person = self.frames.get("person_1")
        if person is not None and self.COL_CASE in person.columns:
            p = person
            if self.COL_G1 in p.columns:
                p = p[p[self.COL_G1] == 0]
            n = len(p)

            def _col(name):
                return p[name].tolist() if name in p.columns else [None] * n

            cids = p[self.COL_CASE].astype(str).tolist()
            si = _col(self.COL_STATED_INCOME)
            it = _col(self.COL_INCOME_TYPE)
            ed = _col(self.COL_EDUCATION)
            ho = _col(self.COL_HOUSING)
            em = _col(self.COL_EMPLOYMENT)
            for i, cid in enumerate(cids):
                if cid in self._declared_by_case:
                    continue
                self._declared_by_case[cid] = _declared_dict(
                    si[i], it[i], ed[i], ho[i], em[i])
            log.info("declared attributes: %d / %d cases populated",
                     sum(1 for v in self._declared_by_case.values() if v),
                     len(self._decision) or n)

    # ------------------------------------------------------------- builders
    def _obligations_from_frame(self, case_id: str, sub: pd.DataFrame) -> List[CanonicalObligation]:
        """Build bureau obligations from ONE sub-frame of a case's payment rows,
        reading the reported DPD (pmts_dpd_1073P / pmts_dpdvalue_108P) and overdue
        amount directly. Columns and the timing source are detected per frame, so
        a_2 (year+month) and b_2 (date) are both handled. Shared by the in-memory
        and streaming paths so the construction is identical either way."""
        cap = self.cfg.dpd_clip_days
        cols = sub.columns
        dpd_col = _first_present(cols, self.DPD_COLS)
        ovd_col = _first_present(cols, self.OVERDUE_COLS)
        date_col = _first_present(cols, self.DATE_COLS)
        year_col = _first_present(cols, self.YEAR_COLS)
        month_col = _first_present(cols, self.MONTH_COLS)
        obligations: List[CanonicalObligation] = []
        for g1, grp in sub.groupby(self.COL_G1, sort=False):
            n = len(grp)
            dpd_vals = grp[dpd_col].tolist() if dpd_col else [None] * n
            ovd_vals = grp[ovd_col].tolist() if ovd_col else [None] * n
            if date_col:
                raw_dates = grp[date_col].tolist()
                years = months = [None] * n
            else:
                raw_dates = [None] * n
                years = grp[year_col].tolist() if year_col else [None] * n
                months = grp[month_col].tolist() if month_col else [None] * n
            payments = []
            for i in range(n):
                d = _to_date(raw_dates[i]) if date_col else _ym_to_date(years[i], months[i])
                if d is None:
                    continue
                payments.append(CanonicalPayment(
                    f"kaggle::{case_id}::{g1}", d, 0.0,
                    overdue=_num_or_zero(ovd_vals[i]),
                    dpd=_clip_dpd(dpd_vals[i], cap)))
            if not payments:
                continue
            obligations.append(CanonicalObligation(
                obligation_id=f"kaggle::{case_id}::{g1}", kind="loan",
                opened=min(p.date for p in payments),
                payments=payments,
            ))
        return obligations

    def _build_obligations(self, case_id: str) -> List[CanonicalObligation]:
        """All in-memory bureau obligations for a case (union over its sub-frames).
        In streaming mode _pay_by_case holds only b_2; a_2 obligations are added
        separately from the streamed chunk."""
        obligations: List[CanonicalObligation] = []
        for sub in self._pay_by_case.get(case_id, []):
            obligations.extend(self._obligations_from_frame(case_id, sub))
        return obligations

    # --------------------------------------------------------- streaming I/O
    def _read_bureau_file(self, fp: str) -> Optional[pd.DataFrame]:
        """Read a single credit_bureau_a_2 chunk frugally: only the columns we
        need, and (in smoke-test mode) with the case-id filter pushed down to the
        parquet reader so we never materialise rows we will not use."""
        import os
        import pyarrow.parquet as pq
        names = pq.ParquetFile(fp).schema.names
        cols = [c for c in self._BUREAU_COLS if c in names]
        filters = None
        if self._keep is not None and self.COL_CASE in names:
            filters = [(self.COL_CASE, "in", self._keep)]
        df = pd.read_parquet(fp, columns=cols, filters=filters)
        log.info("streamed %s rows=%d cols=%d", os.path.basename(fp), len(df), df.shape[1])
        return df

    def _iter_a2_chunks(self):
        """Yield credit_bureau_a_2 as one DataFrame per chunk (never all at once)."""
        if self._a2_test_chunks is not None:
            for df in self._a2_test_chunks:
                yield df
            return
        for fp in self._a2_files:
            df = self._read_bureau_file(fp)
            if df is not None and not df.empty:
                yield df

    def _accounts(self, case_id: str, as_of: date) -> List[CanonicalAccount]:
        bal = self._balance_by_case.get(case_id)
        if bal is None:
            return []
        return [CanonicalAccount(account_id=f"kaggle::{case_id}::card",
                                 type="current", balances=[(as_of, bal)])]

    # --------------------------------------------------------------- public
    def to_canonical(self, limit: Optional[int] = None) -> List[CanonicalApplicant]:
        """Return one CanonicalApplicant per case_id."""
        case_ids = list(self._decision.keys())
        if not case_ids:  # no base table: fall back to union of payment case_ids
            case_ids = sorted(self._pay_by_case.keys())
        if limit is not None:
            case_ids = case_ids[:limit]

        applicants = []
        for i, cid in enumerate(case_ids, 1):
            as_of = self._decision.get(cid, date.today())
            applicants.append(CanonicalApplicant(
                case_id=cid, as_of=as_of,
                obligations=self._build_obligations(cid),
                accounts=self._accounts(cid, as_of),
                monthly_income=self._income_by_case.get(cid),
                instalments=self._instalments_by_case.get(cid, []),
                declared=self._declared_by_case.get(cid, {}),
            ))
            if i % 5000 == 0:
                log.info("  processed %d / %d applicants", i, len(case_ids))
        log.info("kaggle -> %d canonical applicants", len(applicants))
        return applicants

    # ------------------------------------------------- streaming feature build
    def _canonical(self, case_id: str,
                   obligations: List[CanonicalObligation]) -> CanonicalApplicant:
        """Assemble ONE CanonicalApplicant from a case's obligations plus the
        pre-built side indexes. Single source of truth for per-case construction,
        used by to_canonical(), the streaming feature build, and stream_canonical()."""
        as_of = self._decision.get(case_id, date.today())
        return CanonicalApplicant(
            case_id=case_id, as_of=as_of,
            obligations=obligations,
            accounts=self._accounts(case_id, as_of),
            monthly_income=self._income_by_case.get(case_id),
            instalments=self._instalments_by_case.get(case_id, []),
            declared=self._declared_by_case.get(case_id, {}),
        )

    def stream_canonical(self):
        """Yield one CanonicalApplicant per case_id INCLUDING the streamed
        credit_bureau_a_2 obligations, without ever holding a_2 whole.

        Mirrors build_matrix_streaming() but yields the canonical applicant
        (real per-payment DPD/overdue) instead of a feature row, so callers can
        derive ground truth from the real bureau history. Memory is bounded by
        the pushed-down case-id filter (set via from_parquet_dir(max_cases=...)).
        """
        seen = set()
        for df in self._iter_a2_chunks():
            key = df[self.COL_CASE].astype(str)
            for cid, sub in df.groupby(key, sort=False):
                obligations = (self._obligations_from_frame(cid, sub)
                               + self._build_obligations(cid))  # a_2 chunk + b_2
                yield self._canonical(cid, obligations)
                seen.add(cid)
            del df
        remaining = [c for c in self._decision.keys() if c not in seen]
        if not self._decision:  # no base table: fall back to bureau case_ids
            remaining = [c for c in self._pay_by_case.keys() if c not in seen]
        for cid in remaining:
            yield self._canonical(cid, self._build_obligations(cid))
            seen.add(cid)

    def _build_row(self, pipeline, case_id: str,
                   obligations: List[CanonicalObligation]) -> dict:
        """Assemble one CanonicalApplicant and run the SHARED pipeline on it.
        Identical to to_canonical() per-case construction, so features are built
        the same way whether streamed or not."""
        return pipeline.build_row(self._canonical(case_id, obligations))

    def build_matrix_streaming(self, pipeline, flush_every: int = 200000):
        """Memory-safe feature matrix build for the full competition download.

        credit_bureau_a_2 (~188M rows) is streamed one chunk at a time and never
        held whole. Home Credit partitions each depth table by case_id, so a
        case's a_2 rows live entirely inside a single chunk -> we can build that
        case's applicant, run the shared feature pipeline, and discard the raw
        rows before moving on. credit_bureau_b_2 and all side tables are small and
        already in memory (via _pay_by_case / the index dicts), and are merged in
        per case so a case seen by both bureaus keeps all its obligations.

        Returns the same DataFrame shape as FeaturePipeline.build_matrix.
        """
        out_frames: List[pd.DataFrame] = []
        buf: List[dict] = []
        seen = set()

        def flush():
            if buf:
                out_frames.append(pd.DataFrame(buf))
                buf.clear()

        # 1) stream the big a_2 table chunk by chunk
        for df in self._iter_a2_chunks():
            key = df[self.COL_CASE].astype(str)
            for cid, sub in df.groupby(key, sort=False):
                obligations = (self._obligations_from_frame(cid, sub)
                               + self._build_obligations(cid))  # a_2 chunk + b_2
                buf.append(self._build_row(pipeline, cid, obligations))
                seen.add(cid)
            del df
            flush()
            log.info("  streamed chunk -> %d cases built so far", len(seen))

        # 2) every remaining case (b_2-only, or no bureau record at all)
        remaining = [c for c in self._decision.keys() if c not in seen]
        if not self._decision:  # no base table: fall back to bureau case_ids
            remaining = [c for c in self._pay_by_case.keys() if c not in seen]
        for i, cid in enumerate(remaining, 1):
            buf.append(self._build_row(pipeline, cid, self._build_obligations(cid)))
            seen.add(cid)
            if i % flush_every == 0:
                flush()
        flush()

        if not out_frames:
            return pd.DataFrame()
        matrix = (pd.concat(out_frames, ignore_index=True)
                  .set_index("case_id").sort_index())
        log.info("feature matrix: %d rows x %d features",
                 matrix.shape[0], matrix.shape[1])
        return matrix
