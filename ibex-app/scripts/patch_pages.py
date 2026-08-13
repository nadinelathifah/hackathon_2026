#!/usr/bin/env python3
r"""
patch_pages.py -- move the model documentation onto its own page, leave a
teaser on the landing page, and add an FAQ.

Run from the step3 repo root:
    py -3.13 scripts\patch_pages.py

Writes:
  serve/static/model.html            the full model documentation, 9 parts
  serve/web/index.html               teaser + FAQ, nav link repointed

Idempotent. Backup: serve/web/index.html.pages.bak
"""
import os
import sys

MARK = "ibex-pages-v6"


def find_section(html, anchor_id):
    idx = html.find('id="' + anchor_id + '"')
    if idx < 0:
        return None
    start = html.rfind("<section", 0, idx)
    if start < 0:
        return None
    depth = 0
    i = start
    while i < len(html):
        nxt_open = html.find("<section", i + 1)
        nxt_close = html.find("</section>", i + 1)
        if nxt_close < 0:
            return None
        if 0 <= nxt_open < nxt_close:
            depth += 1
            i = nxt_open
        else:
            if depth == 0:
                return (start, nxt_close + len("</section>"))
            depth -= 1
            i = nxt_close
    return None


FEATURES = [
    ("current_clean_streak_24m", "Consecutive months, up to today, with no missed payment on any detected obligation", "Bank feed", "0.15814"),
    ("declared_income_type_code", "How income is earned: salaried, self-employed, pension, benefits or other", "Declared", "0.05715"),
    ("pct_dpd_payments_24m", "Share of scheduled payments in the window that were made late", "Bank feed", "0.02135"),
    ("agg_dpd_count", "Total count of days-past-due events across every detected obligation", "Bank feed", "0.01974"),
    ("declared_employment_code", "Employment status and tenure bucket", "Declared", "0.01806"),
    ("longest_clean_streak_24m", "The longest unbroken run of clean months anywhere in the window", "Bank feed", "0.01549"),
    ("monthly_income", "Median monthly income from detected recurring credits", "Bank feed", "0.01378"),
    ("debt_to_income", "Detected monthly obligations divided by monthly income", "Bank feed", "0.00800"),
    ("total_overdue_amount", "Amount currently past due across all detected obligations", "Bank feed", "0.00343"),
    ("declared_income_gap", "Difference between declared income and income visible in the feed", "Both", "0.00284"),
    ("dpd_late_autocorr_lag1_24m", "Whether a late month tends to be followed by another late month", "Bank feed", "0.00239"),
    ("n_features_missing", "How many inputs could not be computed for this applicant", "Meta", "0.00052"),
    ("declared_provided", "Whether the applicant filled in the declared fields at all", "Meta", "0.00052"),
    ("income_detected", "Whether any recurring income stream was found", "Bank feed", "0.00000"),
    ("thin_file", "Whether the history is too sparse to support the full feature set", "Meta", "0.00000"),
    ("dpd_late_autocorr_lag2_24m", "Two-month-lag version of the arrears persistence measure", "Bank feed", "-0.00002"),
]

ROWS = "".join(
    '<tr><td class="k"><code>' + f[0] + '</code></td><td>' + f[1] +
    '</td><td class="src">' + f[2] + '</td><td class="n">' + f[3] + '</td></tr>'
    for f in FEATURES
)

MIDX_CSS = '''<style>
.midx{background:#F9FAFB;padding:80px 0}
.midx .wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.midx .eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#5FA98C;font-weight:700;margin:0 0 12px}
.midx h2{font-size:40px;line-height:1.12;letter-spacing:-.02em;color:#0B2A20;margin:0 0 16px;font-weight:700}
.midx .lede{font-size:18px;line-height:1.6;color:#4B5563;max-width:68ch;margin:0 0 56px}
.midx h3{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#124A38;font-weight:700;margin:0 0 20px;padding-bottom:12px;border-bottom:1px solid #D4EEE4}
.midx .grp{margin:0 0 64px}
.midx .note{font-size:13.5px;line-height:1.6;color:#6B7280;margin:14px 0 0}
.midx ol.steps{list-style:none;counter-reset:s;margin:0;padding:0}
.midx ol.steps li{counter-increment:s;position:relative;padding:0 0 22px 60px;margin:0 0 22px;border-bottom:1px solid #EFF1F3}
.midx ol.steps li:last-child{border-bottom:0;padding-bottom:0;margin-bottom:0}
.midx ol.steps li::before{content:counter(s);position:absolute;left:0;top:-2px;width:36px;height:36px;border-radius:10px;background:#0B3D2E;color:#D0FF71;font-size:15px;font-weight:700;display:flex;align-items:center;justify-content:center}
.midx ol.steps b{display:block;font-size:17px;color:#111827;margin:0 0 6px;font-weight:650}
.midx ol.steps p{margin:0;font-size:15px;line-height:1.62;color:#4B5563}
.midx .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}
.midx .card{background:#fff;border:1px solid #E5E7EB;border-radius:16px;padding:26px}
.midx .card b{display:block;font-size:16px;color:#111827;margin:0 0 10px;font-weight:650}
.midx .card p{margin:0 0 10px;font-size:14.5px;line-height:1.62;color:#4B5563}
.midx .card p:last-child{margin-bottom:0}
.midx .eq{background:#0B2A20;color:#E8FFF4;border-radius:14px;padding:24px 26px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:15px;line-height:1.7;overflow-x:auto}
.midx .eq span{color:#D0FF71}
.midx table{width:100%;border-collapse:collapse;font-size:14.5px;background:#fff;border:1px solid #E5E7EB;border-radius:14px;overflow:hidden}
.midx th{text-align:left;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:#6B7280;background:#F3F5F7;padding:12px 16px;font-weight:700}
.midx td{padding:13px 16px;border-top:1px solid #EFF1F3;color:#374151;vertical-align:top}
.midx td.k{color:#111827;font-weight:600;white-space:nowrap}
.midx table.feat td{font-size:13.5px;padding:10px 14px}
.midx td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;color:#111827;font-weight:600}
.midx td.src{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#5FA98C;font-weight:700;white-space:nowrap}
.midx code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:#F3F5F7;padding:2px 6px;border-radius:5px;color:#0B3D2E;white-space:nowrap}
.midx .yes{color:#047857;font-weight:650}
.midx .no{color:#B45309;font-weight:650}
.midx .callout{border-left:3px solid #D0FF71;background:#fff;border-radius:0 14px 14px 0;padding:20px 24px;font-size:15px;line-height:1.65;color:#374151;border-top:1px solid #E5E7EB;border-right:1px solid #E5E7EB;border-bottom:1px solid #E5E7EB}
.midx ul.tick{list-style:none;margin:0;padding:0}
.midx ul.tick li{position:relative;padding:0 0 12px 26px;font-size:14.5px;line-height:1.6;color:#4B5563}
.midx ul.tick li::before{content:"";position:absolute;left:0;top:8px;width:7px;height:7px;border-radius:2px;background:#5FA98C}
.midx .flow{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:0 0 24px}
.midx .flow div{background:#fff;border:1px solid #E5E7EB;border-radius:12px;padding:16px;font-size:13.5px;line-height:1.52;color:#4B5563}
.midx .flow b{display:block;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#5FA98C;margin:0 0 7px;font-weight:700}
@media(max-width:640px){.midx h2{font-size:30px}.midx{padding:56px 0}}
</style>'''

BODY = '''<div class="wrap">
<p class="eyebrow">About the model</p>
<h2>How a score is produced, and what it can honestly claim</h2>
<p class="lede">Ibex turns a consented bank-transaction history into a probability of default, then into a score, then into a tamper-evident record. This page documents that path end to end: where the data comes from, the exact features used, the mechanics of the score, how the result is proved, how the error bars are calculated, where bias could enter, and what we would fix next. Every figure is reproducible from the artifacts in the repository.</p>

<div class="grp">
<h3>1 &middot; Where the data comes from</h3>
<div class="flow">
<div><b>Step 1</b>Applicant picks their bank and is redirected to it through TrueLayer.</div>
<div><b>Step 2</b>They authenticate with the bank directly. Ibex never sees the credentials.</div>
<div><b>Step 3</b>The bank returns a one-time authorisation code, exchanged for a short-lived access token.</div>
<div><b>Step 4</b>Ibex pulls accounts, balances, up to 24 months of transactions, and the account-holder record.</div>
<div><b>Step 5</b>Transactions are normalised into dated, signed entries with a counterparty label.</div>
</div>
<div class="cards">
<div class="card"><b>How the data is attached to a person</b><p>The name on the score is not typed in by the applicant. It comes from the account-holder record the bank itself returns, which the bank has already verified under its own KYC obligations.</p><p>That is what makes the score meaningful: an applicant cannot present someone else. The identity, the transaction history and the resulting score all originate from the same authenticated bank session.</p></div>
<div class="card"><b>Consent, and its limits</b><p>Access is scoped to the specific permissions granted -- account details, balances, transactions and identity -- and expires. It is revocable at the bank at any time, without involving Ibex.</p><p>The same normalisation code path serves both the live bank feed and the training data, so a feature computed in production is computed exactly as it was in training.</p></div>
</div>
</div>

<div class="grp">
<h3>2 &middot; The pipeline, end to end</h3>
<ol class="steps">
<li><b>Data intake</b><p>1,526,659 applications, each with a 24-month repayment history. The live open-banking feed enters the same code path as the training data, so nothing is computed one way in research and another way in production.</p></li>
<li><b>Feature construction</b><p>A recurring-payment detector groups debits by counterparty and rounded amount, then keeps only clusters that behave like obligations: a minimum number of payments, a median gap of 5 to 40 days, and an amount coefficient of variation at or below 0.35. Surviving streams are classified as loan, card or BNPL. From those, and from the declared fields, 74 candidate features are built.</p></li>
<li><b>Parity verification</b><p>Every feature from the open-banking path is checked against the reference implementation value for value, at a tolerance of 1e-6 absolute plus 1e-4 relative, with both-missing counted as agreement. Mean match rate is 0.93. Anything that cannot be reproduced from a bank feed is refused, whatever its predictive power.</p></li>
<li><b>Feature selection</b><p>Four gates in order: null-importance screening against 30 label-shuffled runs at the 95th percentile, a correlation prune at 0.90, an information-value floor of 0.005, then permutation importance on validation. 74 candidates reduce to 16.</p></li>
<li><b>Training</b><p>Gradient-boosted trees (LightGBM), 132 rounds, native missing-value handling, monotone constraints where the economic direction is unambiguous. Chosen over XGBoost and a blend on calibration-set Gini, never on the evaluation set.</p></li>
<li><b>Calibration</b><p>Raw model output is not a probability. Isotonic regression on a held-out 305,332-row slice maps rank to calibrated risk. Brier falls from 0.1789 to 0.0263; expected calibration error is 0.0057.</p></li>
<li><b>Scoring and proof</b><p>The calibrated probability becomes a score, the floor and ceiling are applied, reason codes are attached, and the score event is hashed and anchored on chain.</p></li>
</ol>
</div>

<div class="grp">
<h3>3 &middot; The exact features in the model</h3>
<p style="font-size:16px;line-height:1.6;color:#4B5563;margin:0 0 24px">All sixteen, with the drop in evaluation Gini when that feature alone is randomly permuted. Higher means the model leans on it more.</p>
<table class="feat">
<thead><tr><th>Feature</th><th>What it measures</th><th>Source</th><th>Importance</th></tr></thead>
<tbody>''' + ROWS + '''</tbody>
</table>
<p class="note"><b>Bank feed</b> is derived from transactions; <b>Declared</b> is stated by the applicant and optional; <b>Meta</b> describes the completeness of the data rather than the applicant. The last three sit at or below zero, meaning they add nothing measurable to ranking on their own -- they are retained as explicit flags so that missing or sparse data is modelled openly rather than silently imputed. The top feature carries the largest share of importance by a wide margin, which is itself listed as a model risk below.</p>
</div>

<div class="grp">
<h3>4 &middot; The mechanics</h3>
<div class="eq">score = <span>427.1229</span> + <span>57.7078</span> &times; ln( (1 &minus; PD) / PD )</div>
<p style="font-size:14.5px;line-height:1.62;color:#4B5563;margin:16px 0 24px">The multiplier is 40 / ln(2), so every doubling of the odds of repayment adds exactly 40 points. The offset anchors a score of 600 to odds of 20:1. The gap between two scores is therefore a statement about odds, not an arbitrary ranking.</p>
<div class="cards">
<div class="card"><b>The probability floor</b><p>The model may not claim a risk below 0.916%. That is not a preference, it is where the evidence runs out: in the safest bucket only a few hundred applicants with a handful of defaults support the estimate, so a Beta posterior over that bucket sets the lowest defensible claim.</p><p>The floor is fixed policy; its value is re-derived at every retrain.</p></div>
<div class="card"><b>The score ceiling</b><p>Because the floor caps how low PD can go, it caps the score at 697.4. About 0.17% of applicants reach it, so it is a genuine limit on claims rather than a bunching artefact.</p><p>A consequence worth stating plainly: band A cannot currently be reached. Awarding a top band would require evidence this data does not contain.</p></div>
<div class="card"><b>Bands</b><p>A from 720, B from 660, C from 600, D from 540, E below. Boundaries are fixed on the odds scale and are not rebased at retrain, so a B this year means the same odds as a B last year.</p></div>
</div>
</div>

<div class="grp">
<h3>5 &middot; Proof: hashing and the smart contract</h3>
<p style="font-size:16px;line-height:1.6;color:#4B5563;margin:0 0 24px">The chain is used for one job only: proving that a score existed, in a specific form, at a specific time, from a specific model. No personal data, no transactions and no score value are written to it.</p>
<table>
<thead><tr><th>Anchored value</th><th>What it is</th><th>What it proves</th></tr></thead>
<tbody>
<tr><td class="k"><code>userHash</code></td><td>Salted hash of the bank-verified identity</td><td>The record belongs to one specific person, without publishing who they are</td></tr>
<tr><td class="k"><code>scoreEventHash</code></td><td>Hash of the score event: score, band, PD, timestamp and a digest of the feature vector</td><td>The result has not been altered after the fact</td></tr>
<tr><td class="k"><code>modelVersionHash</code></td><td>Hash of the model artifacts that produced it</td><td>Which model version issued the score, so an old score cannot be attributed to a newer model</td></tr>
<tr><td class="k"><code>merkleRoot</code></td><td>Root of the batch the event was included in</td><td>Many events can be anchored in one transaction without weakening any individual proof</td></tr>
<tr><td class="k"><code>txHash</code></td><td>The anchoring transaction itself</td><td>An independently checkable timestamp</td></tr>
</tbody>
</table>
<div class="cards" style="margin-top:20px">
<div class="card"><b>How a lender verifies</b><p>The applicant hands over their score-event file. The lender pastes it into the verification page; the server recomputes the hash and compares it against the value anchored on chain. A match means the file is byte-for-byte what was issued.</p><p>Verification is pull-based and consent-first: the lender can only check a record the applicant has chosen to share.</p></div>
<div class="card"><b>Why hashes rather than data</b><p>A hash is one-way. Publishing it proves a document existed without revealing anything inside it, so the chain can be public while the financial data stays in the encrypted database.</p><p>The chain adds no predictive power. It is an integrity mechanism, not part of the model.</p></div>
</div>
</div>

<div class="grp">
<h3>6 &middot; Is it unbiased?</h3>
<p style="font-size:16px;line-height:1.6;color:#4B5563;margin:0 0 24px">Bias is handled as a register of specific risks with a specific status, not as a single claim.</p>
<table>
<thead><tr><th>Risk</th><th>Status</th><th>Detail</th></tr></thead>
<tbody>
<tr><td class="k">Protected attributes</td><td class="yes">Excluded</td><td>No age, sex, ethnicity, nationality, marital status or postcode enters the model at any stage.</td></tr>
<tr><td class="k">Education</td><td class="yes">Excluded by policy</td><td>Available and mildly predictive; dropped because it proxies socio-economic background more than repayment behaviour.</td></tr>
<tr><td class="k">Proxy leakage</td><td class="no">Acknowledged</td><td>Income, housing and employment tenure correlate with protected characteristics. They are retained because they are directly causal for affordability, but omitting the protected field does not remove the correlation.</td></tr>
<tr><td class="k">Perverse monotonicity</td><td class="yes">Constrained</td><td>Monotone constraints stop the model ever learning that more arrears or a shorter clean streak improves a score.</td></tr>
<tr><td class="k">Outcome fairness testing</td><td class="no">Not performed</td><td>Disparate-impact ratios across protected groups have not been computed, because the data carries no protected labels to test against. A genuine gap, stated rather than glossed.</td></tr>
<tr><td class="k">Thin-file penalty</td><td class="yes">Measured</td><td>A thin-file flag is carried explicitly, so absence of history is treated as absence of evidence rather than evidence of risk.</td></tr>
</tbody>
</table>
</div>

<div class="grp">
<h3>7 &middot; Is it fair?</h3>
<div class="cards">
<div class="card"><b>Every decision is explained</b><p>Each score carries the specific features that pushed it down and those that held it up, with their contributions, for that applicant rather than an average.</p></div>
<div class="card"><b>Declared fields are optional</b><p>Employment, income type and housing can be left blank without penalty. Whether the applicant declared at all is carried as its own flag, so a blank is never silently read as a negative.</p></div>
<div class="card"><b>Decision support, not a verdict</b><p>Ibex returns a probability and a band. It declines no one. The lending decision, and the policy behind it, stays with the lender.</p></div>
<div class="card"><b>Honest limits</b><p>The service publishes its own ceiling and its own error bars next to every score. A model that cannot say how uncertain it is should not be trusted with the decision.</p></div>
</div>
</div>

<div class="grp">
<h3>8 &middot; Stability, and how the error bars are calculated</h3>
<div class="cards">
<div class="card"><b>Measured across 32 origination weeks</b><p>The observed default rate drifts from 3.35% to 2.13% across the evaluation window. That is right-censoring -- later cohorts have had less time to default -- not the model decaying.</p></div>
<div class="card"><b>Design effect 2.20&times;</b><p>Applications arrive in weekly cohorts sharing an economy and a credit policy, so they are not independent. Resampling whole weeks inflates variance by 4.86&times;, making the honest standard error 2.20&times; the naive one. 305,332 evaluation rows carry the information of roughly 62,833 independent ones.</p></div>
<div class="card"><b>The stability figure</b><p>Per-vintage Gini is computed for every week with enough defaults to support it. A stable model gives a flat series with small spread; a drifting one shows trend. It is recomputed at each retrain and reported with the model, not asserted once.</p></div>
</div>

<p style="font-size:16px;line-height:1.6;color:#4B5563;margin:36px 0 20px">Every interval printed beside a score comes from the same six-step procedure. The important choice is the second one: what gets resampled.</p>
<ol class="steps">
<li><b>Divide the evaluation set into blocks</b><p>The 305,332 evaluation rows fall into 56 weekly origination cohorts. A cohort shares a macroeconomic environment, a marketing mix and a credit policy, so two applicants from the same week are more alike than two drawn at random.</p></li>
<li><b>Resample whole weeks, not individual rows</b><p>Draw 56 blocks with replacement to build a synthetic evaluation set of the same size. Resampling rows would silently assume every application is independent, which is the assumption that fails here. This single choice is what separates the honest standard error from the flattering one.</p></li>
<li><b>Refit the calibrator on every replicate</b><p>The isotonic calibration is re-estimated from scratch inside each replicate. Reusing one calibrator would treat the calibration step as error-free and understate the uncertainty on every PD.</p></li>
<li><b>Recompute every published quantity</b><p>On each replicate: Gini, the PD at scores 600, 660 and 697, the probability floor, and the tail counts below the floor knot. Nothing is derived analytically from another quantity.</p></li>
<li><b>Repeat 500 times, then read percentiles</b><p>The 2.5th and 97.5th percentiles of the replicate distribution give the interval. Not the point estimate plus or minus 1.96 standard errors: the Gini distribution is skewed by +0.25 and the floor distribution by +1.30, so a symmetric interval would put both ends in the wrong place.</p></li>
<li><b>Map PD bounds through the score formula</b><p>The score interval is the PD interval pushed through score = 427.1229 + 57.7078 ln((1 &minus; PD)/PD). Because that transform is non-linear, the resulting score interval is asymmetric around the point score -- correctly so.</p></li>
</ol>

<table style="margin-top:32px">
<thead><tr><th>Quantity</th><th>How its uncertainty is estimated</th><th>Value</th></tr></thead>
<tbody>
<tr><td class="k">Gini, analytic</td><td>DeLong standard error. Assumes independent observations, so it is reported only for contrast.</td><td class="n">0.0060</td></tr>
<tr><td class="k">Gini, iid bootstrap</td><td>Resamples individual rows with replacement, 500 replicates. Agrees with DeLong, which confirms the gap below is clustering rather than a coding difference.</td><td class="n">0.0061</td></tr>
<tr><td class="k">Gini, block bootstrap</td><td>Resamples whole origination weeks. <b>This is the figure quoted everywhere in the product.</b></td><td class="n">0.0135</td></tr>
<tr><td class="k">PD at a given score</td><td>Percentile interval across the 500 block replicates, each with its own refitted calibrator.</td><td class="n">per score</td></tr>
<tr><td class="k">Score interval</td><td>The PD bounds mapped through the points-to-double-the-odds formula.</td><td class="n">asymmetric</td></tr>
<tr><td class="k">Probability floor</td><td>Jeffreys Beta posterior over the safest bucket; where a bucket contains zero defaults the rule of three gives roughly 3/n as the upper bound.</td><td class="n">0.61% to 1.81%</td></tr>
<tr><td class="k">Tail evidence</td><td>Bootstrap over the count of observations below the floor knot.</td><td class="n">1825 obs, 13 defaults</td></tr>
</tbody>
</table>
<div class="callout" style="margin-top:24px">Discrimination on the evaluation set is a Gini of <b>0.3835</b>, with a 95% block-bootstrap confidence interval of <b>0.3613 to 0.4146</b>. The clustered standard error is 0.0135 against a DeLong standard error of 0.0060 -- quoting the smaller of the two would overstate precision by more than a factor of two.</div>
</div>

<div class="grp" style="margin-bottom:0">
<h3>9 &middot; Limitations and what comes next</h3>
<div class="cards">
<div class="card"><b>Known limitations</b><ul class="tick"><li>Declared fields cannot be verified from a bank feed alone, so they are usable for pricing but not for underwriting on their own.</li><li>One feature carries the largest share of measured importance by a wide margin; concentration that high is a model risk in itself.</li><li>Training labels come only from accepted applicants, so the model has never observed how rejected ones would have behaved.</li><li>Balance trajectories and income-regime shifts are not available in the current feed, capping how much behavioural signal can be extracted.</li></ul></div>
<div class="card"><b>Planned work</b><ul class="tick"><li>Hyperparameter search, expected to add roughly 0.005 to 0.015 Gini on the current feature set.</li><li>A point-in-time audit of the dominant feature, to confirm no information from after the decision date leaks into it.</li><li>Promote per-vintage stability from a reported diagnostic to a term in model selection, so a slightly weaker but steadier model can win.</li><li>Reject inference, to correct the acceptance bias in the training labels.</li><li>Population stability monitoring in production, alerting when live feature distributions drift from training.</li><li>Disparate-impact testing as soon as a dataset with protected labels can lawfully be used for it.</li></ul></div>
</div>
</div>

</div>'''

MODEL_PAGE = '''<!doctype html>
<html lang="en" data-build="''' + MARK + '''">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>How the model works &middot; Ibex Credit</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#F9FAFB;color:#111827;-webkit-font-smoothing:antialiased}
.mnav{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);border-bottom:1px solid #E5E7EB}
.mnav .in{max-width:1080px;margin:0 auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between}
.mnav a.brand{font-weight:750;font-size:17px;color:#0B2A20;text-decoration:none;letter-spacing:-.01em}
.mnav .lk{display:flex;gap:22px;align-items:center}
.mnav .lk a{font-size:14.5px;color:#4B5563;text-decoration:none}
.mnav .lk a:hover{color:#0B3D2E}
.mnav .cta{background:#0B3D2E;color:#fff !important;padding:9px 16px;border-radius:10px;font-weight:600}
.mfoot{border-top:1px solid #E5E7EB;background:#fff;padding:28px 24px;text-align:center;font-size:13px;color:#6B7280}
</style>
''' + MIDX_CSS + '''
</head>
<body>
<nav class="mnav"><div class="in"><a class="brand" href="/">Ibex Credit</a><div class="lk"><a href="/">Home</a><a href="/#faq">FAQ</a><a class="cta" href="/static/signup.html">Run the demo</a></div></div></nav>
<section id="model-in-depth" class="midx">''' + BODY + '''</section>
<footer class="mfoot">&copy; 2026 Ibex Credit. Demonstration uses synthetic data only. Not a lending decision.</footer>
</body>
</html>'''

TEASER = '''<section id="model-in-depth" class="mtz" data-build="''' + MARK + '''">
<style>
.mtz{background:#0B2A20;padding:84px 0}
.mtz .wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.mtz .eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#5FA98C;font-weight:700;margin:0 0 14px}
.mtz h2{font-size:38px;line-height:1.14;letter-spacing:-.02em;color:#fff;margin:0 0 16px;font-weight:700;max-width:20ch}
.mtz p.l{font-size:17px;line-height:1.62;color:#B9CFC5;max-width:62ch;margin:0 0 36px}
.mtz .st{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:18px;margin:0 0 36px}
.mtz .st div{border-top:1px solid #1E5745;padding:16px 0 0}
.mtz .st b{display:block;font-size:28px;color:#D0FF71;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.mtz .st small{display:block;font-size:12.5px;color:#8FB3A6;margin-top:4px;line-height:1.4}
.mtz a.go{display:inline-block;background:#D0FF71;color:#0B2A20;font-weight:650;font-size:15px;padding:14px 24px;border-radius:12px;text-decoration:none}
.mtz a.go:hover{background:#B9E85E}
@media(max-width:640px){.mtz h2{font-size:28px}}
</style>
<div class="wrap">
<p class="eyebrow">About the model</p>
<h2>How a score is produced, and what it can honestly claim</h2>
<p class="l">The full documentation covers where the data comes from, all sixteen features, the scoring mechanics, how the result is proved on chain, how the error bars are calculated, and where the model falls short.</p>
<div class="st">
<div><b>0.3835</b><small>Gini, 95% CI 0.3613 to 0.4146</small></div>
<div><b>16</b><small>features, from 74 candidates</small></div>
<div><b>305,332</b><small>evaluation rows, 32 weeks</small></div>
<div><b>697.4</b><small>score ceiling, PD floor 0.92%</small></div>
</div>
<a class="go" href="/static/model.html">Read the full model documentation &rarr;</a>
</div>
</section>'''

FAQ = '''<section id="faq" class="ifq">
<style>
.ifq{background:#F9FAFB;padding:84px 0;border-top:1px solid #E5E7EB}
.ifq .wrap{max-width:820px;margin:0 auto;padding:0 24px}
.ifq .eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#5FA98C;font-weight:700;margin:0 0 12px;text-align:center}
.ifq h2{font-size:36px;line-height:1.15;letter-spacing:-.02em;color:#0B2A20;margin:0 0 44px;font-weight:700;text-align:center}
.ifq details{background:#fff;border:1px solid #E5E7EB;border-radius:14px;margin:0 0 12px;overflow:hidden}
.ifq details[open]{border-color:#D4EEE4}
.ifq summary{cursor:pointer;padding:19px 54px 19px 24px;font-size:16px;font-weight:600;color:#111827;list-style:none;position:relative}
.ifq summary::-webkit-details-marker{display:none}
.ifq summary::after{content:"+";position:absolute;right:24px;top:17px;font-size:21px;font-weight:400;color:#5FA98C}
.ifq details[open] summary::after{content:"\\2212"}
.ifq .a{padding:0 24px 20px;font-size:15px;line-height:1.66;color:#4B5563}
.ifq .a p{margin:0 0 10px}
.ifq .a p:last-child{margin:0}
.ifq .more{text-align:center;margin:32px 0 0;font-size:14.5px;color:#6B7280}
.ifq .more a{color:#0B3D2E;font-weight:600}
@media(max-width:640px){.ifq h2{font-size:27px}}
</style>
<div class="wrap">
<p class="eyebrow">FAQ</p>
<h2>Questions people actually ask</h2>

<details><summary>Do I need a UK credit history?</summary><div class="a"><p>No. That is the point of the service. The score is built from how you have actually handled money over the last 24 months in your bank account, not from a credit file you may not have.</p><p>If you have recently moved to the UK and no lender can see you, this gives you something to show them.</p></div></details>

<details><summary>Will this affect my credit file?</summary><div class="a"><p>No. Nothing is recorded with a credit reference agency, no search is left on your file, and Ibex is not itself a credit reference agency. Running a score here is invisible to any other lender unless you choose to share the result.</p></div></details>

<details><summary>What exactly can you see in my bank account?</summary><div class="a"><p>Account details, balances, up to 24 months of transactions, and the account-holder record your bank holds for you. Access is read-only: no payment can be initiated, and nothing can be moved.</p></div></details>

<details><summary>Do you store my banking login?</summary><div class="a"><p>No, and we never see it. You authenticate on your own bank\u2019s website. The bank returns a scoped, expiring token to us, which grants exactly the permissions you approved and nothing else.</p></div></details>

<details><summary>Can I withdraw access later?</summary><div class="a"><p>Yes, at any time, from your bank\u2019s own app or website, without asking us. Open-banking consent is time-limited by design and lapses on its own if it is not renewed.</p></div></details>

<details><summary>Why is my score capped? Why can nobody get band A?</summary><div class="a"><p>The model refuses to claim a risk lower than 0.92%, because below that level the data contains too few comparable applicants to support the claim. That floor caps the score at 697.4.</p><p>Band A starts at 720, so it is currently unreachable. That is deliberate. Awarding a top band would mean asserting a level of certainty the evidence does not carry.</p></div></details>

<details><summary>Why does declared information matter if you cannot verify it?</summary><div class="a"><p>It adds context the transactions alone do not carry, such as whether income is salaried or self-employed. It is genuinely predictive, which is why it is in the model.</p><p>It is also optional, and leaving it blank is recorded as its own fact rather than treated as a negative. Because it cannot be verified from a bank feed, it is suitable for pricing but should not carry an underwriting decision on its own.</p></div></details>

<details><summary>What is actually stored on the blockchain?</summary><div class="a"><p>Only hashes. Your name, your transactions and even your score are never written to the chain. A hash proves a specific record existed at a specific time without revealing anything inside it.</p><p>It means a lender can confirm a score has not been edited, while your financial data stays in the encrypted database.</p></div></details>

<details><summary>Is this a lending decision?</summary><div class="a"><p>No. Ibex returns a probability and a band and declines no one. What a lender does with that, and the policy behind it, remains entirely theirs.</p></div></details>

<details><summary>How accurate is it, honestly?</summary><div class="a"><p>A Gini of 0.3835, with a 95% confidence interval of 0.3613 to 0.4146. That is respectable for a model built without any credit-bureau data, and well short of what a major lender achieves with a full credit file.</p><p>Every score is published with its own interval rather than a single number, because a model that cannot state its own uncertainty should not be trusted with the decision.</p></div></details>

<details><summary>What if I have barely any transaction history?</summary><div class="a"><p>A thin-file flag is set and carried through the model explicitly, so a short history is treated as missing evidence rather than as evidence of risk. You will still get a score, with a wider interval around it.</p></div></details>

<p class="more">Still curious? The <a href="/static/model.html">full model documentation</a> covers the pipeline, every feature and the statistics.</p>
</div>
</section>'''


def write_model_page():
    d = os.path.join("serve", "static")
    if not os.path.isdir(d):
        print("ERROR %s missing -- run from the repo root" % d)
        return False
    p = os.path.join(d, "model.html")
    open(p, "w", encoding="utf-8").write(MODEL_PAGE)
    print("OK    wrote %s (%d bytes, 9 parts, %d features)" % (p, len(MODEL_PAGE), len(FEATURES)))
    return True


def patch_index():
    p = os.path.join("serve", "web", "index.html")
    if not os.path.exists(p):
        print("ERROR %s not found" % p)
        return False
    html = open(p, encoding="utf-8").read()
    if MARK in html:
        print("OK    landing already on " + MARK)
        return True
    bak = p + ".pages.bak"
    if not os.path.exists(bak):
        open(bak, "w", encoding="utf-8").write(html)

    span = find_section(html, "model-in-depth")
    if span:
        html = html[:span[0]] + TEASER + "\n" + FAQ + html[span[1]:]
        print("OK    in-page model section -> teaser + FAQ (%d bytes replaced)" % (span[1] - span[0]))
    else:
        anchor = html.rfind("<footer")
        if anchor < 0:
            anchor = html.lower().rfind("</body>")
        if anchor < 0:
            print("ERROR nowhere to insert on %s" % p)
            return False
        html = html[:anchor] + TEASER + "\n" + FAQ + "\n" + html[anchor:]
        print("OK    no model section found -- appended teaser + FAQ before the footer")

    n = html.count('href="#model-in-depth"')
    html = html.replace('href="#model-in-depth"', 'href="/static/model.html"')
    print("OK    repointed %d nav link(s) to /static/model.html" % n)
    open(p, "w", encoding="utf-8").write(html)
    return True


if __name__ == "__main__":
    a = write_model_page()
    b = patch_index()
    print("")
    print("model page: %s   landing: %s" % ("ok" if a else "FAILED", "ok" if b else "FAILED"))
    sys.exit(0 if (a and b) else 1)
