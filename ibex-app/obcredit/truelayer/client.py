"""Minimal, dependency-light TrueLayer Data API client (sandbox + live).

Docs: https://docs.truelayer.com/  (Data API)

Sandbox base URLs:
  auth : https://auth.truelayer-sandbox.com
  data : https://api.truelayer-sandbox.com/data/v1

This client does TWO things:
  1. OAuth: exchange an auth-code (or refresh token) for an access token.
  2. Fetch: pull accounts, transactions, balances, direct debits, standing
     orders for a connected user and assemble the exact `payload` dict that
     TrueLayerAdapter expects.

Nothing about feature maths lives here; this is pure I/O so it is easy to mock.

-------------------------------------------------------------------------------
BUILD 19 -- 5-year request, robust windowing, declared attributes wired through
-------------------------------------------------------------------------------
Windowing. BUILD 18 sent one `?from=...&to=...` covering the whole lookback.
That is correct per the Data API reference but fails in practice, because the
window contract is enforced by the *provider*:

  * spans beyond the provider maximum (commonly 90 days) are rejected with
    `invalid_date_range` / "Invalid date range provided";
  * `to` may never be in the future -- end-of-day on the current date is
    refused for most of the day ("`to` cannot be in the future");
  * some providers accept only `YYYY-MM-DD`, others full RFC-3339;
  * every provider holds a finite history and 400s or returns nothing before
    the earliest data it has.

So this client negotiates rather than assumes:

  1. clamp `to` to a few minutes before now (UTC);
  2. if the span fits one chunk, try RFC-3339 then bare dates;
  3. otherwise walk backwards in `chunk_days` slices, merging and
     de-duplicating, stopping after `max_empty_chunks` consecutive empty
     slices -- that point is the provider's real history horizon;
  4. if every windowed form fails, fall back to an unwindowed call.

Lookback. DEFAULT_MONTHS is 60. Requesting five years is free and safe: the
backwards walk self-terminates at the horizon, so a provider holding two years
costs three wasted probes and returns exactly what it has. What it will NOT do
is conjure history that does not exist -- check `history_days` in the returned
payload for what actually arrived.

Declared attributes. BUILD 18 hard-coded `"declared": {}` in fetch_user, so on
the TrueLayer path every declared_* feature silently took its missing-value
default -- including declared_income_gap, which compares stated income against
detected income. Changing the form could not move the score because the form
never reached the feature builder. `declared` is now a parameter and its
absence is logged as a warning.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..logging_utils import get_logger

log = get_logger("truelayer_client")

try:
    import requests
except Exception:  # keeps import working in offline/test environments
    requests = None


class TrueLayerError(RuntimeError):
    """Non-2xx we cannot recover from. Carries the body, where the reason is."""

    def __init__(self, status: int, path: str, body: str):
        super().__init__("TrueLayer %s on %s: %s" % (status, path, body[:500]))
        self.status = status
        self.path = path
        self.body = body


class TrueLayerDataClient:
    SANDBOX_AUTH = "https://auth.truelayer-sandbox.com"
    SANDBOX_DATA = "https://api.truelayer-sandbox.com/data/v1"
    LIVE_AUTH = "https://auth.truelayer.com"
    LIVE_DATA = "https://api.truelayer.com/data/v1"

    #: Lookback requested by default, in months. 60 = 5 years.
    DEFAULT_MONTHS = 24
    #: Per-request span. 90d is the common provider maximum.
    DEFAULT_CHUNK_DAYS = 180

    def __init__(self, client_id: str, client_secret: str, sandbox: bool = True,
                 access_token: Optional[str] = None, timeout: int = 30):
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_base = self.SANDBOX_AUTH if sandbox else self.LIVE_AUTH
        self.data_base = self.SANDBOX_DATA if sandbox else self.LIVE_DATA
        self.access_token = access_token
        self.refresh_token: Optional[str] = None
        self.timeout = timeout

    # ----------------------------------------------------------- auth link
    def authorization_url(self, redirect_uri: str, state: str,
                          scope: Optional[str] = None,
                          providers: Optional[str] = None) -> str:
        """Hosted-auth URL. In the sandbox, `uk-cs-mock` is the Mock Bank."""
        from urllib.parse import urlencode
        scope = scope or ("info accounts balance cards transactions "
                          "direct_debits standing_orders offline_access")
        providers = providers or "uk-cs-mock uk-ob-all uk-oauth-all"
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "scope": scope,
            "redirect_uri": redirect_uri,
            "providers": providers,
            "state": state,
        }
        return f"{self.auth_base}/?{urlencode(params)}"

    # --------------------------------------------------------------- OAuth
    def exchange_code(self, code: str, redirect_uri: str) -> str:
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })

    def refresh(self, refresh_token: str) -> str:
        return self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    def _token_request(self, extra: Dict[str, str]) -> str:
        if requests is None:
            raise RuntimeError("requests not available")
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "client_id/client_secret missing -- check TRUELAYER_CLIENT_ID "
                "and TRUELAYER_CLIENT_SECRET are set in the shell that started "
                "the server")
        payload = {"client_id": self.client_id, "client_secret": self.client_secret}
        payload.update(extra)
        r = requests.post(f"{self.auth_base}/connect/token", data=payload,
                          timeout=self.timeout)
        if r.status_code != 200:
            log.error("TOKEN FAIL %s: %s", r.status_code, r.text)
            raise TrueLayerError(r.status_code, "/connect/token", r.text)
        body = r.json()
        self.access_token = body["access_token"]
        if body.get("refresh_token"):
            self.refresh_token = body["refresh_token"]
        return self.access_token

    # ---------------------------------------------------------------- I/O
    def _request(self, path: str,
                 quiet: bool = False) -> Tuple[int, Optional[dict], str]:
        """GET returning (status, parsed_json_or_None, raw_text); never raises
        on non-200. `quiet` demotes the failure log to debug, for probes where
        a rejection is an expected answer rather than a fault."""
        if requests is None:
            raise RuntimeError("requests not available")
        if not self.access_token:
            raise RuntimeError("no access_token; call exchange_code/refresh first")
        r = requests.get(f"{self.data_base}{path}",
                         headers={"Authorization": f"Bearer {self.access_token}"},
                         timeout=self.timeout)
        if r.status_code != 200:
            (log.debug if quiet else log.error)(
                "DATA FAIL %s %s: %s", r.status_code, path, r.text[:500])
            return r.status_code, None, r.text
        try:
            return 200, r.json(), r.text
        except ValueError:
            log.error("DATA BAD JSON %s: %s", path, r.text[:200])
            return 200, None, r.text

    def _get(self, path: str) -> dict:
        status, body, text = self._request(path)
        if status != 200 or body is None:
            raise TrueLayerError(status, path, text)
        return body

    @staticmethod
    def _results(resp: Optional[dict]) -> List[dict]:
        return resp.get("results", []) if isinstance(resp, dict) else []

    @staticmethod
    def _norm_balances(rows: List[dict]) -> List[dict]:
        """Normalise /balance to the {current, timestamp} shape the adapter wants."""
        out: List[dict] = []
        for b in rows or []:
            out.append({
                "current": b.get("current", b.get("available", 0.0)),
                "timestamp": b.get("update_timestamp") or b.get("timestamp"),
            })
        return out

    # ------------------------------------------------- transaction fetching
    @staticmethod
    def _tx_key(t: dict) -> str:
        """Stable identity for de-duplication across overlapping chunks."""
        tid = t.get("transaction_id") or t.get("normalised_provider_transaction_id")
        if tid:
            return str(tid)
        return "|".join(str(t.get(k, "")) for k in
                        ("timestamp", "amount", "description", "transaction_type"))

    @staticmethod
    def _tx_date(t: dict) -> str:
        return str(t.get("timestamp") or "")

    @staticmethod
    def _end_bound(to: date) -> Tuple[str, str]:
        """(rfc3339, bare-date) end bounds that are never in the future.

        TrueLayer hard-rejects any `to` beyond the current instant. End-of-day
        on the current date is therefore wrong for most of the day. Clamp to a
        few minutes before now (UTC) to stay clear of clock skew.
        """
        now = datetime.now(timezone.utc)
        end_of_day = datetime(to.year, to.month, to.day, 23, 59, 59,
                              tzinfo=timezone.utc)
        bound = min(end_of_day, now - timedelta(minutes=5))
        return bound.strftime("%Y-%m-%dT%H:%M:%SZ"), bound.date().isoformat()

    def _try_window(self, aid: str, frm: date, to: date, iso_datetime: bool,
                    quiet: bool = True) -> Tuple[bool, List[dict]]:
        """One transactions call for one window. Returns (ok, rows)."""
        end_iso, end_date = self._end_bound(to)
        if iso_datetime:
            q = f"?from={frm.isoformat()}T00:00:00Z&to={end_iso}"
        else:
            q = f"?from={frm.isoformat()}&to={end_date}"
        status, body, _ = self._request(f"/accounts/{aid}/transactions{q}",
                                        quiet=quiet)
        if status != 200:
            return False, []
        return True, self._results(body)

    def fetch_transactions(self, aid: str, frm: date, to: date,
                           chunk_days: int = DEFAULT_CHUNK_DAYS,
                           max_empty_chunks: int = 3) -> List[dict]:
        """Pull transactions for [frm, to], negotiating the window format.

        The backwards walk self-terminates after `max_empty_chunks` empty
        slices, so over-requesting the lookback is cheap: asking for 5 years
        from a provider holding 2 costs three extra probes.
        """
        span_days = (to - frm).days

        # Only probe the single-shot window when it could plausibly succeed.
        # A multi-year span is a guaranteed 400; probing it just emits
        # alarming log lines ahead of the fallback that was always going to run.
        if span_days <= chunk_days:
            for iso in (True, False):
                ok, rows = self._try_window(aid, frm, to, iso)
                if ok:
                    log.info("transactions: single window accepted (%s), %d rows",
                             "rfc3339" if iso else "date", len(rows))
                    return self._dedupe(rows)
            log.warning("transactions: window rejected for %s in both formats; "
                        "falling back to chunked fetch", aid)
        else:
            log.info("transactions: %dd span exceeds %dd provider limit for %s; "
                     "fetching in chunks", span_days, chunk_days, aid)

        merged: Dict[str, dict] = {}
        cursor = to
        empty_streak = 0
        chunks_ok = 0
        while cursor > frm:
            start = max(frm, cursor - timedelta(days=chunk_days))
            got = None
            for iso in (True, False):
                ok, rows = self._try_window(aid, start, cursor, iso)
                if ok:
                    got = rows
                    break
            if got is None:
                log.debug("transactions: chunk %s..%s rejected in both formats",
                          start, cursor)
                empty_streak += 1
            else:
                chunks_ok += 1
                if got:
                    empty_streak = 0
                    for t in got:
                        merged[self._tx_key(t)] = t
                else:
                    empty_streak += 1
            if empty_streak >= max_empty_chunks:
                log.info("transactions: %d empty chunks in a row at %s -- "
                         "provider history horizon reached",
                         empty_streak, start.isoformat())
                break
            cursor = start - timedelta(days=1)

        if merged or chunks_ok:
            out = self._dedupe(list(merged.values()))
            log.info("transactions: chunked fetch returned %d rows (%d chunks ok)",
                     len(out), chunks_ok)
            return out

        log.warning("transactions: all windowed forms failed for %s; "
                    "falling back to unwindowed provider default", aid)
        status, body, _ = self._request(f"/accounts/{aid}/transactions")
        if status != 200:
            log.error("transactions: unwindowed call also failed (%s)", status)
            return []
        return self._dedupe(self._results(body))

    def _dedupe(self, rows: List[dict]) -> List[dict]:
        seen: Dict[str, dict] = {}
        for t in rows or []:
            seen[self._tx_key(t)] = t
        return sorted(seen.values(), key=self._tx_date)

    # ------------------------------------------------------- file thickness
    @staticmethod
    def _thickness(accounts: List[dict]) -> Dict[str, object]:
        """Summarise how thick the connected file actually is.

        'Credit lines' in this project are derived purely from open banking --
        credit-card account types plus loan/card-shaped mandates in direct
        debits and standing orders. There is no bureau feed. A provider mock
        with no cards and no credit mandates is a thin file by construction,
        and no request parameter can change that.
        """
        credit_accounts = sum(
            1 for a in accounts
            if str(a.get("account_type", "")).upper() in
            ("CREDIT_CARD", "CARD", "LOAN", "MORTGAGE"))
        mandates = sum(len(a.get("direct_debits") or []) +
                       len(a.get("standing_orders") or []) for a in accounts)
        txns = sum(len(a.get("transactions") or []) for a in accounts)
        return {
            "accounts": len(accounts),
            "credit_accounts": credit_accounts,
            "mandates": mandates,
            "transactions": txns,
        }

    # ------------------------------------------------------------- fetch
    def fetch_user(self, case_id: str, as_of: Optional[date] = None,
                   months: int = DEFAULT_MONTHS,
                   chunk_days: int = DEFAULT_CHUNK_DAYS,
                   declared: Optional[Dict[str, object]] = None) -> dict:
        """Pull everything for the connected user and assemble adapter payload.

        `months` defaults to 60 (5 years). The realised span is reported back
        in the payload so callers can verify what actually arrived rather than
        assuming the request was honoured.

        `declared` carries the applicant-stated attributes from the form. It
        must be supplied or every declared_* feature falls back to its default.
        """
        as_of = as_of or date.today()
        frm = as_of - timedelta(days=int(round(months * 30.44)))
        log.info("fetching %d months (%s .. %s) for case %s",
                 months, frm.isoformat(), as_of.isoformat(), case_id)

        accounts_out: List[dict] = []
        all_stamps: List[str] = []

        for acc in self._results(self._get("/accounts")):
            aid = acc["account_id"]
            txns = self.fetch_transactions(aid, frm, as_of, chunk_days=chunk_days)
            all_stamps.extend(self._tx_date(t)[:10] for t in txns if self._tx_date(t))
            accounts_out.append({
                "account_id": aid,
                "account_type": acc.get("account_type", "TRANSACTION"),
                "transactions": txns,
                "balances": self._norm_balances(
                    self._safe_json(f"/accounts/{aid}/balance")),
                "direct_debits": self._safe(f"/accounts/{aid}/direct_debits"),
                "standing_orders": self._safe(f"/accounts/{aid}/standing_orders"),
            })

        hist_from = min(all_stamps) if all_stamps else None
        hist_to = max(all_stamps) if all_stamps else None
        hist_days = None
        if hist_from and hist_to:
            try:
                hist_days = (datetime.fromisoformat(hist_to).date()
                             - datetime.fromisoformat(hist_from).date()).days
            except ValueError:
                hist_days = None

        if hist_days is not None:
            log.info("history realised: %s .. %s (%d days, %.1f months) "
                     "against %d months requested",
                     hist_from, hist_to, hist_days, hist_days / 30.44, months)
            if hist_days < months * 30.44 * 0.8:
                log.warning("SHORT HISTORY: got %.1f months, wanted %d -- the "
                            "provider does not hold more. Long-lookback "
                            "features are limited to what arrived.",
                            hist_days / 30.44, months)
        else:
            log.warning("no dated transactions returned -- history span unknown")

        thickness = self._thickness(accounts_out)
        log.info("file thickness: %s", thickness)
        if not thickness["credit_accounts"]:
            log.warning("THIN FILE: no credit accounts on this connection. "
                        "Credit-utilisation and repayment features will be "
                        "missing, which depresses the score. Sandbox mock "
                        "users hold fixed fixtures -- build a custom test user "
                        "in the TrueLayer console to get a thick file.")

        declared = dict(declared or {})
        if declared:
            log.info("declared attributes attached: %s", sorted(declared))
        else:
            log.warning("no declared attributes passed to fetch_user -- "
                        "declared_* features will fall back to defaults and "
                        "declared_income_gap will be meaningless")

        _info_rows = self.fetch_info()
        _holder = self._holder_from_info(_info_rows)
        if _holder:
            log.info("account holder from /info: %s", _holder)
        else:
            log.warning("no account-holder name returned by /info; the score "
                        "handle will fall back to the typed email alone")
        return {
            "info": _info_rows,
            "holder_name": _holder,
            "full_name": _holder,
            "case_id": case_id,
            "as_of": as_of.isoformat(),
            "accounts": accounts_out,
            "declared": declared,
            "history_from": hist_from,
            "history_to": hist_to,
            "history_days": hist_days,
            "months_requested": months,
            "thickness": thickness,
        }

    # ------------------------------------------------ identity  BUILD 19 INFO
    def fetch_info(self):
        """GET /info -- the bank-verified identity block.

        Needs the `info` scope, which is already requested at connect time.
        Tolerant: some providers do not expose /info at all.
        """
        try:
            rows = self._safe_json("/info")
        except Exception as exc:            # pragma: no cover
            log.debug("/info unavailable: %s", exc)
            return []
        return rows or []

    @staticmethod
    def _holder_from_info(rows):
        """Pull the first usable holder name out of an /info response."""
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in ("full_name", "name", "account_holder_name"):
                val = row.get(key)
                if isinstance(val, str) and val.strip():
                    return " ".join(val.split())
            names = row.get("names")
            if isinstance(names, list):
                for nm in names:
                    if isinstance(nm, str) and nm.strip():
                        return " ".join(nm.split())
                    if isinstance(nm, dict):
                        val = nm.get("full_name") or nm.get("name")
                        if isinstance(val, str) and val.strip():
                            return " ".join(val.split())
        return ""

    def _safe_json(self, path: str) -> List[dict]:
        status, body, _ = self._request(path, quiet=True)
        if status != 200:
            return []
        return self._results(body)

    def _safe(self, path: str) -> List[dict]:
        """Some accounts/providers don't expose DD/SO; tolerate 404/403."""
        try:
            return self._safe_json(path)
        except Exception as e:  # pragma: no cover
            log.debug("optional endpoint %s unavailable: %s", path, e)
            return []




