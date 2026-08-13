"""
Keccak-256 and canonical JSON, matching the Node chain repo byte for byte.

The chain repo derives the on-chain identity as

    userHash = keccak256(utf8(userId + ":" + USER_SALT))

see utils/hashScoreEvent.js line 51, where hashUtf8 is
ethers.keccak256(ethers.toUtf8Bytes(v)).

The Python side previously used v3.make_handle(), which is

    sha256(IBEX_APP_SALT | email | normalise_name(bank_name))

Different algorithm, different salt, different inputs. The two could never
agree, so every business lookup returned a clean-looking "not found".

No third-party dependency: requirements.txt is deliberately stdlib-only
apart from the data stack. Keccak-f[1600] is implemented here and checked
against published vectors by selftest().
"""

import json

_MASK = (1 << 64) - 1

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

_ROTC = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl(v, n):
    if n == 0:
        return v
    return ((v << n) | (v >> (64 - n))) & _MASK


def _keccak_f(A):
    for rnd in range(24):
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            dx = D[x]
            for y in range(5):
                A[x][y] ^= dx

        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rotl(A[x][y], _ROTC[x][y])

        for x in range(5):
            for y in range(5):
                A[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y] & _MASK) & B[(x + 2) % 5][y])

        A[0][0] ^= _RC[rnd]
    return A


_RATE = 136  # bytes, Keccak-256


def keccak256(data: bytes) -> bytes:
    A = [[0] * 5 for _ in range(5)]

    # Keccak padding is 0x01 .. 0x80. NOT SHA3's 0x06 -- hashlib.sha3_256
    # uses the latter and will NOT reproduce ethers.keccak256.
    padlen = _RATE - (len(data) % _RATE)
    if padlen == 1:
        data = data + b"\x81"
    else:
        data = data + b"\x01" + b"\x00" * (padlen - 2) + b"\x80"

    for off in range(0, len(data), _RATE):
        blk = data[off:off + _RATE]
        for i in range(_RATE // 8):
            lane = int.from_bytes(blk[i * 8:(i + 1) * 8], "little")
            A[i % 5][i // 5] ^= lane
        _keccak_f(A)

    out = b""
    for i in range(_RATE // 8):
        out += A[i % 5][i // 5].to_bytes(8, "little")
    return out[:32]


def keccak256_hex(text: str) -> str:
    """ethers.keccak256(ethers.toUtf8Bytes(text))"""
    return "0x" + keccak256(text.encode("utf-8")).hex()


def _js_number(v):
    """
    JSON.stringify number formatting. This matters: score-event.json holds
    "confidence": 0.0, which JS emits as 0 and Python's json.dumps emits as
    0.0 -- a one-character difference that changes scoreEventHash entirely.
    """
    if v != v or v in (float("inf"), float("-inf")):
        return "null"
    if float(v) == int(v) and abs(v) < 1e21:
        return str(int(v))
    return repr(float(v))


def canonical_json(value) -> str:
    """Port of canonicalJson() in utils/hashScoreEvent.js: recursive, keys sorted."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _js_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = [json.dumps(k, ensure_ascii=False) + ":" + canonical_json(value[k])
                 for k in sorted(value.keys())]
        return "{" + ",".join(parts) + "}"
    raise TypeError("cannot canonicalise %r" % type(value))


def user_hash(user_id: str, salt: str) -> str:
    """keccak256(utf8(userId + ':' + USER_SALT)) -- hashScoreEvent.js:51"""
    return keccak256_hex(user_id + ":" + salt)


def score_event_hash(event: dict) -> str:
    return keccak256_hex(canonical_json(event))


def model_version_hash(model_version: str) -> str:
    return keccak256_hex(model_version)


def selftest() -> None:
    """Published Keccak-256 vectors. Raises if the implementation drifts."""
    cases = [
        ("", "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
        ("abc", "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
    ]
    for text, want in cases:
        got = keccak256_hex(text)
        if got != want:
            raise AssertionError("keccak256(%r) = %s, expected %s" % (text, got, want))


selftest()


def configured_salt(chain_dir: str = "") -> str:
    """
    Resolve USER_SALT. It must be byte-identical to the chain repo's .env, or
    every hash we produce is a well-formed hash of nobody and every lookup
    returns a clean-looking "not found". Prefer the environment; otherwise
    read the chain repo directly so the two cannot drift apart.
    """
    import os

    v = os.environ.get("USER_SALT", "").strip()
    if v:
        return v
    path = os.path.join(chain_dir, ".env") if chain_dir else ""
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("USER_SALT="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return ""


_TITLES = {"MR", "MRS", "MISS", "MS", "DR", "PROF", "SIR", "MX", "REV"}


def normalise_name(raw: str) -> str:
    """
    Strip accents, punctuation, titles and spacing so the same human hashes
    identically across banks. Mirrors ibex_v3.normalise_name.
    """
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z ]", " ", s).upper()
    return " ".join(p for p in s.split() if p and p not in _TITLES)


def chain_user_id(email: str, bank_name: str = "") -> str:
    """
    The userId string written into score-event.json.

    This is PLAIN TEXT, not a hash. hashScoreEvent.js applies
    keccak256(userId + ":" + salt) itself, so anything pre-hashed here would
    be hashed twice and could never be looked up again.

    Binding the bank-verified name into the identity is what lets a business
    verify a name rather than just an email address.
    """
    e = (email or "").strip().lower()
    n = normalise_name(bank_name)
    return (e + "|" + n) if n else e


def chain_user_hash(email: str, bank_name: str, salt: str) -> str:
    """The on-chain userHash for this person. Must equal the Node result."""
    return user_hash(chain_user_id(email, bank_name), salt)
