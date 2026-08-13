"""Central, auditable configuration.

Every magic number a credit-risk reviewer might question lives here, with a
rationale. Changing behaviour = changing this file, not the maths code.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    # ---- recurring-stream detection (TrueLayer side) ----
    min_payments_for_stream: int = 3        # need >=3 events to call something recurring
    period_min_days: int = 20               # tighter than 'monthly' to allow weekly/4-weekly
    period_max_days: int = 400              # up to ~annual obligations
    amount_cv_max: float = 0.35             # coefficient of variation of amounts within a stream
    amount_round_dp: int = 0                # rounding when grouping candidate amounts

    # ---- income detection (TrueLayer side: recurring salary inflow) ----
    # Income is the dominant RECURRING credit stream, not a keyword median. These
    # knobs decide what counts as a genuine, material, recurring inflow.
    income_min_events: int = 3              # >=3 credits before a stream is 'recurring'
    income_period_min_days: int = 5         # weekly ...
    income_period_max_days: int = 40        # ... up to a monthly salary cadence
    income_amount_cv_max: float = 0.35      # salary amounts are fairly stable
    income_min_monthly: float = 100.0       # ignore tiny recurring credits (noise floor)

    # ---- cyclical-payment / DPD model (the 'outlier method') ----
    ontime_abs_tol_days: int = 3            # |deviation| <= this is treated as on-time
    ontime_rel_tol: float = 0.10            # OR within 10% of the cadence period
    missed_dpd_cap_days: int = 90           # a fully missed cycle is capped at this DPD
    robust_z_threshold: float = 3.5         # MAD robust z above which a payment is a late-outlier
    mad_scale: float = 1.4826               # makes MAD a consistent estimator of sigma (normal)

    # ---- delinquency (days-past-due primitive, read DIRECTLY from the bureau) ----
    # DPD is the core signal. On the Kaggle side it is the bureau-reported DPD
    # (pmts_dpd_1073P / pmts_dpdvalue_108P); on open banking it is reconstructed
    # from transaction timing by PaymentScheduleModel. We CAP DPD on both sides:
    #   * robust to the bureau's absurd outliers (b_2 max ~1.8e8 is not days);
    #   * aligns with the Basel 90-days-past-due default definition -- beyond 90
    #     DPD the account is treated as defaulted, so capping loses no ranking.
    dpd_clip_days: float = 90.0             # cap applied to DPD on BOTH sides
    dpd_serious_threshold: float = 30.0     # DPD >= this counts as 'serious arrears'

    # ---- delinquency (overdue-amount primitive) ----
    overdue_amount_tol: float = 1.0         # overdue above this (currency units) counts as arrears

    # ---- feature windows ----
    default_window_months: int = 24         # lookback for windowed delinquency features
    short_window_months: int = 3            # lookback for liquidity features

    # ---- parity testing ----
    parity_abs_tol: float = 1e-6            # two features 'match' if |a-b| <= this
    parity_rel_tol: float = 1e-4


DEFAULT = EngineConfig()
