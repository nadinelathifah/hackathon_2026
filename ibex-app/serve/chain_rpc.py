"""
Direct Ethereum JSON-RPC client for the Ibex score audit registry.

No node runtime and no web3 dependency: the registry's ABI surface is tiny,
signing is RFC 6979 deterministic ECDSA over secp256k1 (implemented here and
validated by selftest() against the published EIP-155 test vector), and the
keccak comes from serve.chain_hash, which is byte-identical to the chain
repo's ethers.keccak256 -- proven against the live contract.

Reads (verification) need only an RPC endpoint. Writes (anchoring)
additionally need PRIVATE_KEY in the environment. This lets Render's
python-only service anchor and verify without the hardhat project; when the
chain repo is checked out locally the caller may still prefer the hardhat
scripts.

Only legacy (type-0) transactions are built: Polygon supports them, and the
calldata path is identical to what submitScoreRoot.js sends.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from serve.chain_hash import keccak256

DEFAULT_CONTRACT = "0x8621D09F08C2f58803e7239F8D46D444e0eF63e1"
DEFAULT_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://polygon-rpc.com",
)


class ChainRevert(Exception):
    """A contract revert or transport failure with an HTTP-ish status hint."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------
def _clean_hex(raw: str, what: str, length: int) -> str:
    """Tolerate dashboard pastes: invisible characters, quotes, a copied
    polygonscan URL, a missing 0x prefix. Returns lowercase 0x-hex."""
    for prefix in ("0x", "0X"):
        if prefix in raw:
            raw = raw.split(prefix, 1)[1]
            break
    raw = "".join(c for c in raw if c in "0123456789abcdefABCDEF")
    if len(raw) != length:
        raise ChainRevert(
            f"{what} does not look right ({len(raw)} hex characters, "
            f"expected {length}) -- check the value in the service "
            f"environment", 500)
    return "0x" + raw.lower()


def contract_address() -> str:
    raw = (os.environ.get("SCORE_AUDIT_V2_CONTRACT_ADDRESS")
           or os.environ.get("IBEX_CONTRACT")
           or DEFAULT_CONTRACT).strip()
    return _clean_hex(raw, "the contract address", 40)


_logged_config = False


def _log_config_once(sender: str = "") -> None:
    """One startup-style line in the service log so a misconfigured deploy
    is diagnosable from the logs alone."""
    global _logged_config
    if not _logged_config:
        print(f"chain_rpc: contract={contract_address()} "
              f"rpc={_rpc_urls()[0]} sender={sender or '(read-only)'}")
        _logged_config = True


def _rpc_urls() -> List[str]:
    primary = (os.environ.get("POLYGON_RPC_URL")
               or os.environ.get("IBEX_RPC_URL") or "").strip()
    urls = ([primary] if primary else []) + list(DEFAULT_RPCS)
    seen: List[str] = []
    for u in urls:
        if u and u not in seen:
            seen.append(u)
    return seen


def read_available() -> bool:
    """Verification only needs an endpoint; defaults are built in."""
    return bool(_rpc_urls())


def write_available() -> bool:
    """Anchoring additionally needs the issuer key in the environment."""
    return read_available() and bool(os.environ.get("PRIVATE_KEY", "").strip())


# ----------------------------------------------------------------------
# JSON-RPC transport with endpoint failover
# ----------------------------------------------------------------------
def _rpc(method: str, params: list, timeout: int = 20) -> Any:
    import requests

    last: Optional[Exception] = None
    for url in _rpc_urls():
        try:
            r = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": method, "params": params},
                timeout=timeout)
            body = r.json()
            if "error" in body:
                err = body["error"]
                raise ChainRevert(
                    _decode_revert(err.get("data") or "",
                                   err.get("message") or "rpc error"))
            return body.get("result")
        except ChainRevert:
            raise
        except Exception as exc:  # transport problem: try the next endpoint
            last = exc
    raise ChainRevert(f"every Polygon RPC endpoint failed: {last}", 503)


# ----------------------------------------------------------------------
# secp256k1 (affine, RFC 6979 deterministic nonces, low-s, self-verifying)
# ----------------------------------------------------------------------
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G = (_GX, _GY)
_Point = Optional[Tuple[int, int]]


def _p_add(a: _Point, b: _Point) -> _Point:
    if a is None:
        return b
    if b is None:
        return a
    ax, ay = a
    bx, by = b
    if ax == bx and (ay + by) % _P == 0:
        return None
    if a == b:
        m = (3 * ax * ax) * pow(2 * ay, -1, _P) % _P
    else:
        m = (by - ay) * pow(bx - ax, -1, _P) % _P
    x = (m * m - ax - bx) % _P
    return x, (m * (ax - x) - ay) % _P


def _p_mul(k: int, point: _Point = _G) -> _Point:
    out: _Point = None
    add = point
    while k:
        if k & 1:
            out = _p_add(out, add)
        add = _p_add(add, add)
        k >>= 1
    return out


def _rfc6979_k(priv: int, msg_hash: bytes) -> int:
    """Deterministic nonce per RFC 6979 with HMAC-SHA256 (32-byte hash)."""
    bx = priv.to_bytes(32, "big")
    bh = msg_hash
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + bx + bh, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + bx + bh, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < _N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def pubkey_from_priv(priv: int) -> Tuple[int, int]:
    q = _p_mul(priv)
    assert q is not None
    return q


def address_from_priv(priv: int) -> str:
    q = pubkey_from_priv(priv)
    return "0x" + keccak256(
        q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big"))[-20:].hex()


def _recover(msg_hash: bytes, r: int, s: int, recid: int) -> Tuple[int, int]:
    """ecrecover: return the signer public key point."""
    x = r + (recid >> 1) * _N
    if x >= _P:
        raise ChainRevert("bad recovery id", 500)
    alpha = (pow(x, 3, _P) + 7) % _P
    y = pow(alpha, (_P + 1) // 4, _P)
    if (y & 1) != (recid & 1):
        y = _P - y
    z = int.from_bytes(msg_hash, "big")
    rinv = pow(r, -1, _N)
    # Q = r^-1 (sR - zG)
    sr = _p_mul(s, (x, y))
    zg = _p_mul(z % _N, _G)
    neg_zg = (zg[0], (-zg[1]) % _P) if zg else None
    q = _p_mul(rinv, _p_add(sr, neg_zg))
    assert q is not None
    return q


def _sign_hash(msg_hash: bytes, priv: int) -> Tuple[int, int, int]:
    """Return (recid, r, s). Self-verifies via ecrecover before returning."""
    z = int.from_bytes(msg_hash, "big")
    k = _rfc6979_k(priv, msg_hash)
    rx, ry = _p_mul(k)  # type: ignore[misc]
    r = rx % _N
    recid = (ry & 1) | (1 if rx >= _N else 0)
    s = pow(k, -1, _N) * (z + r * priv) % _N
    if s > _N // 2:  # EIP-2 low-s
        s = _N - s
        recid ^= 1
    # Never emit a signature we cannot recover ourselves from.
    if _recover(msg_hash, r, s, recid) != pubkey_from_priv(priv):
        raise ChainRevert("internal signing error", 500)
    return recid, r, s


# ----------------------------------------------------------------------
# RLP
# ----------------------------------------------------------------------
def _rlp(item: Any) -> bytes:
    if isinstance(item, int):
        item = b"" if item == 0 else item.to_bytes(
            (item.bit_length() + 7) // 8, "big")
    elif isinstance(item, str):
        item = bytes.fromhex(item[2:] if item.startswith("0x") else item)
    if isinstance(item, (bytes, bytearray)):
        b = bytes(item)
        if len(b) == 1 and b[0] < 0x80:
            return b
        return _rlp_len(len(b), 0x80) + b
    payload = b"".join(_rlp(x) for x in item)
    return _rlp_len(len(payload), 0xC0) + payload


def _rlp_len(n: int, offset: int) -> bytes:
    if n <= 55:
        return bytes([offset + n])
    size = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(size)]) + size


# ----------------------------------------------------------------------
# ABI
# ----------------------------------------------------------------------
def _selector(sig: str) -> bytes:
    return keccak256(sig.encode())[:4]


def _word32(raw: bytes) -> bytes:
    assert len(raw) <= 32
    return b"\x00" * (32 - len(raw)) + raw


def _hex32(value: str) -> bytes:
    v = value[2:] if value.startswith("0x") else value
    assert len(v) == 64, f"expected a 32 byte hash, got {value!r}"
    return bytes.fromhex(v)


def _encode_call(sig: str, words: List[bytes]) -> str:
    return "0x" + (_selector(sig) + b"".join(_word32(w) for w in words)).hex()


# ----------------------------------------------------------------------
# custom error decoding (V2 reverts with typed errors, not strings)
# ----------------------------------------------------------------------
def _err_sel(sig: str) -> str:
    return "0x" + keccak256(sig.encode())[:4].hex()


_ERRORS = {
    _err_sel("ScoreUpdateTooSoon(uint256)"): "ScoreUpdateTooSoon",
    _err_sel("ScorePeriodNotNewer(uint32,uint32)"): "ScorePeriodNotNewer",
    _err_sel("ScoreEventHashAlreadyUsed(bytes32)"): "ScoreEventHashAlreadyUsed",
    _err_sel("DailyIssuerSubmissionLimitReached(address,uint256)"):
        "DailyIssuerSubmissionLimitReached",
    _err_sel("NotApprovedIssuer(address)"): "NotApprovedIssuer",
    _err_sel("EnforcedPause()"): "EnforcedPause",
    _err_sel("InvalidScorePeriod(uint32)"): "InvalidScorePeriod",
    _err_sel("InvalidHash(string)"): "InvalidHash",
}


def _decode_revert(data: str, fallback: str) -> str:
    if not data or not data.startswith("0x") or len(data) < 10:
        return fallback
    sel = data[:10]
    name = _ERRORS.get(sel)
    if not name:
        if sel == "0x08c379a0":  # Error(string)
            try:
                raw = bytes.fromhex(data[10:])
                ln = int.from_bytes(raw[32:64], "big")
                return raw[64:64 + ln].decode("utf-8", "replace")
            except Exception:
                pass
        return fallback
    arg = int(data[10:10 + 64], 16) if len(data) >= 74 else 0
    if name == "ScoreUpdateTooSoon":
        when = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(arg)) if arg else "later"
        return (f"the contract rate limit rejected this anchor: this identity "
                f"may update once every 28 days, next allowed {when}. The "
                f"existing record is still fully verifiable on the business "
                f"page.")
    if name == "ScorePeriodNotNewer":
        return ("the contract requires a strictly newer YYYYMM score period "
                "for an update; this identity is already anchored for a "
                "month at least this new.")
    if name == "ScoreEventHashAlreadyUsed":
        return "this exact score event has already been anchored."
    if name == "DailyIssuerSubmissionLimitReached":
        return "the issuer wallet has hit its daily submission limit."
    if name == "NotApprovedIssuer":
        return ("the PRIVATE_KEY wallet is not an approved issuer on the "
                "registry contract.")
    if name == "EnforcedPause":
        return "the registry contract is paused."
    return f"contract reverted with {name}"


# ----------------------------------------------------------------------
# reads
# ----------------------------------------------------------------------
def read_record(user_hash: str) -> Dict[str, Any]:
    """latestRecordByUserHash(bytes32). Returns {"ok","log","rec"} with rec
    shaped exactly like the _scrape_hashes output the hardhat read produced:
    zero values become None / "0" so callers' existence checks keep working."""
    _log_config_once()
    data = _encode_call("latestRecordByUserHash(bytes32)", [_hex32(user_hash)])
    ret = _rpc("eth_call", [{"to": contract_address(), "data": data}, "latest"])
    raw = bytes.fromhex((ret or "0x")[2:])
    if len(raw) < 192:
        raise ChainRevert(f"short registry response ({len(raw)} bytes)", 502)
    words = [raw[i * 32:(i + 1) * 32] for i in range(6)]
    seh, root, mvh = ("0x" + w.hex() for w in words[:3])
    ts = int.from_bytes(words[3], "big")
    period = int.from_bytes(words[4], "big")
    issuer = "0x" + words[5][-20:].hex()
    zero = set(seh) == {"0", "x"}

    def _hz(h: str) -> Optional[str]:
        return None if set(h) == {"0", "x"} else h

    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""
    rec = {
        "userHash": user_hash,
        "scoreEventHash": _hz(seh),
        "merkleRoot": _hz(root),
        "modelVersionHash": _hz(mvh),
        "timestampEpoch": str(ts),
        "timestamp": iso,
        "scorePeriod": period or None,
        "issuer": None if zero else issuer,
        "score": None,
        "band": None,
        "txHash": None,
    }
    period_txt = (f"{period // 100}-{period % 100:02d}") if period else "0"
    log = ("(direct JSON-RPC read -- no node runtime)\n"
           f"userHash: {user_hash}\n"
           f"scoreEventHash: {seh}\n"
           f"merkleRoot: {root}\n"
           f"modelVersionHash: {mvh}\n"
           f"scorePeriod: {period_txt}\n"
           f"timestamp: {ts} ({iso})\n"
           f"issuer: {issuer}\n")
    return {"ok": True, "log": log, "rec": rec}


def next_submission_at(user_hash: str) -> int:
    data = _encode_call("nextSubmissionAt(bytes32)", [_hex32(user_hash)])
    ret = _rpc("eth_call", [{"to": contract_address(), "data": data}, "latest"])
    return int((ret or "0x0"), 16)


def approved_issuer(address: str) -> bool:
    data = _encode_call("approvedIssuers(address)",
                        [bytes.fromhex(address[2:])])
    ret = _rpc("eth_call", [{"to": contract_address(), "data": data}, "latest"])
    return int((ret or "0x0"), 16) == 1


def is_paused() -> bool:
    data = _encode_call("paused()", [])
    ret = _rpc("eth_call", [{"to": contract_address(), "data": data}, "latest"])
    return int((ret or "0x0"), 16) == 1


def issuer_daily_usage(address: str) -> Tuple[int, int, int]:
    data = _encode_call("issuerDailyUsage(address)",
                        [bytes.fromhex(address[2:])])
    ret = _rpc("eth_call", [{"to": contract_address(), "data": data}, "latest"])
    raw = bytes.fromhex((ret or "0x")[2:])
    if len(raw) < 96:
        return 0, 0, 0
    return (int.from_bytes(raw[0:32], "big"),
            int.from_bytes(raw[32:64], "big"),
            int.from_bytes(raw[64:96], "big"))


# ----------------------------------------------------------------------
# write
# ----------------------------------------------------------------------
def _private_key() -> int:
    raw = os.environ.get("PRIVATE_KEY", "").strip()
    cleaned = _clean_hex(raw, "PRIVATE_KEY", 64)
    return int(cleaned, 16)


def submit_score_root(user_hash: str, score_event_hash: str,
                      merkle_root: str, model_version_hash: str,
                      score_period: int) -> Tuple[str, str]:
    """Sign and send submitScoreRoot. Returns (tx_hash, log_text).
    Raises ChainRevert with a decoded, human message on contract reverts."""
    priv = _private_key()
    sender = address_from_priv(priv)
    _log_config_once(sender)

    if is_paused():
        raise ChainRevert("the registry contract is paused", 502)
    if not approved_issuer(sender):
        raise ChainRevert(
            f"the configured wallet {sender} is not an approved issuer on "
            f"the registry", 502)
    _day, used, remaining = issuer_daily_usage(sender)

    data = _encode_call(
        "submitScoreRoot(bytes32,bytes32,bytes32,bytes32,uint32)",
        [_hex32(user_hash), _hex32(score_event_hash), _hex32(merkle_root),
         _hex32(model_version_hash), score_period.to_bytes(4, "big")])

    to = contract_address()
    chain_id = int(_rpc("eth_chainId", []), 16)
    nonce = int(_rpc("eth_getTransactionCount", [sender, "pending"]), 16)
    gas_price = int(int(_rpc("eth_gasPrice", []), 16) * 125 // 100)

    # estimate first: a revert here is the contract talking, so decode it.
    try:
        est = int(_rpc("eth_estimateGas",
                       [{"from": sender, "to": to, "data": data}]), 16)
    except ChainRevert as exc:
        msg = str(exc)
        status = 429 if ("28 days" in msg or "score period" in msg) else 502
        raise ChainRevert(msg, status)
    gas = est * 120 // 100

    tx = [nonce, gas_price, gas, bytes.fromhex(to[2:]), 0,
          bytes.fromhex(data[2:])]
    sighash = keccak256(_rlp(tx + [chain_id, 0, 0]))
    recid, r, s = _sign_hash(sighash, priv)
    v = chain_id * 2 + 35 + recid
    raw = "0x" + _rlp(tx + [v, r, s]).hex()

    tx_hash = _rpc("eth_sendRawTransaction", [raw], timeout=30)
    log = [
        "Score event source: direct JSON-RPC (no hardhat runtime)",
        f"userHash: {user_hash}",
        f"scoreEventHash: {score_event_hash}",
        f"modelVersionHash: {model_version_hash}",
        f"merkleRoot: {merkle_root}",
        f"Score period: {score_period // 100}-{score_period % 100:02d}",
        f"Issuer daily usage before submission: {used} used, "
        f"{remaining} remaining",
        f"Transaction sent: {tx_hash}",
    ]

    deadline = time.time() + 120
    receipt = None
    while time.time() < deadline:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            break
        time.sleep(2.5)
    if not receipt:
        raise ChainRevert(
            f"transaction {tx_hash} was sent but not mined within 120s -- "
            f"check polygonscan before retrying", 504)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise ChainRevert(
            f"transaction {tx_hash} mined but reverted on chain", 502)
    log.append("Score proof anchored on Polygon mainnet")
    log.append(f"PolygonScan link: https://polygonscan.com/tx/{tx_hash}}}")
    return tx_hash, "\n".join(log)


# ----------------------------------------------------------------------
# misc
# ----------------------------------------------------------------------
def score_period_of(ev: Dict[str, Any]) -> int:
    """resolveScorePeriod(): SCORE_PERIOD override, else YYYYMM of the
    event's UTC timestamp."""
    override = os.environ.get("SCORE_PERIOD", "").strip()
    if override:
        if not (len(override) == 6 and override.isdigit()):
            raise ChainRevert("SCORE_PERIOD must be YYYYMM", 500)
        period = int(override)
    else:
        ts = str(ev.get("timestamp", ""))
        try:
            period = int(ts[0:4]) * 100 + int(ts[5:7])
        except Exception:
            raise ChainRevert(
                f"cannot derive a score period from timestamp {ts!r}", 500)
    year, month = period // 100, period % 100
    if year < 2020 or month < 1 or month > 12:
        raise ChainRevert(f"invalid score period {period}", 500)
    return period


def selftest() -> None:
    """Vectors: RLP (ethereum wiki), the EIP-155 signing example, and the
    latestRecordByUserHash selector observed on the live contract."""
    assert _rlp(b"dog").hex() == "83646f67"
    assert _rlp(1024).hex() == "820400"
    assert _rlp([]).hex() == "c0"
    assert _rlp([[], [[]], [[], [[]]]]).hex() == "c7c0c1c0c3c0c1c0"
    assert _rlp(0).hex() == "80"

    # The EIP-155 worked example signs a known transaction with a known key.
    priv = int("46464646464646464646464646464646464646464646464646464646"
               "46464646", 16)
    tx = [9, 20 * 10**9, 21000,
          bytes.fromhex("3535353535353535353535353535353535353535"),
          10**18, b""]
    assert _rlp(tx + [1, 0, 0]).hex() == (
        "ec098504a817c800825208943535353535353535353535353535353535353535"
        "880de0b6b3a764000080018080")
    sighash = keccak256(_rlp(tx + [1, 0, 0]))
    recid, r, s = _sign_hash(sighash, priv)
    assert recid == 0, recid
    assert r == int("28ef61340bd939bc2195fe537567866003e1a15d3c71ff63e1590620"
                    "aa636276", 16), hex(r)
    assert s == int("67cbe9d8997f761aecb703304b3800ccf555c9f3dc64214b297fb19"
                    "66a3b6d83", 16), hex(s)
    raw = "0x" + _rlp(tx + [37, r, s]).hex()
    assert raw == (
        "0xf86c098504a817c800825208943535353535353535353535353535353535353"
        "535880de0b6b3a76400008025a028ef61340bd939bc2195fe537567866003e1a15"
        "d3c71ff63e1590620aa636276a067cbe9d8997f761aecb703304b3800ccf555c9f"
        "3dc64214b297fb1966a3b6d83"), raw

    assert _selector("latestRecordByUserHash(bytes32)").hex() == "f78ba53b"


if __name__ == "__main__":  # pragma: no cover
    selftest()
    print("chain_rpc selftest OK")
