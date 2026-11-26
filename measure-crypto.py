"""
Measure the cost of the security mechanisms on the evaluation machine.

  AEAD ciphers     AES-256-GCM and ChaCha20-Poly1305, encryption and decryption, for
                   message sizes like our partitions and results (1 KB to 4 MB).
                   Each operation is fitted as  t(n) = fixed + n / rate.
  Handshake CPU    TLS 1.3 key exchange and certificate signature, both sides:
                     x25519          2 key pairs + 2 shared secrets + Ed25519 sign + verify
                     x25519mlkem768  the same plus ML-KEM-768 keygen, encaps and decaps
                                     (hybrid post-quantum key exchange, FIPS 203)
                   The network round trip of the handshake is added by the simulator.

One thread, Python `cryptography` package (OpenSSL). Run on the same machine as all
other measurements, from the project folder:
    python measure-crypto.py

Output: measurements/crypto.json (used by extensions.py) and measurements/crypto.csv
"""
import csv
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import cryptography
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "measurements"
SIZES = [1_000, 4_000, 16_000, 64_000, 256_000, 1_000_000, 4_000_000]
ROUNDS = 7           # median of this many rounds
MIN_ROUND_S = 0.2    # each round repeats the operation for at least this long


def per_op(fn) -> float:
    """Median time of one call of fn, in seconds."""
    fn()                                   # warm-up
    n = 1
    while True:                            # find a repeat count that lasts MIN_ROUND_S
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        if time.perf_counter() - t0 >= MIN_ROUND_S:
            break
        n *= 2
    rounds = []
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        rounds.append((time.perf_counter() - t0) / n)
    return statistics.median(rounds)


def fit(sizes, times) -> dict:
    """Least-squares fit t = fixed + n / rate, on relative error (weights 1/t), so that
    small messages, which dominate our transfers, weigh as much as large ones."""
    t = np.array(times, float)
    slope, fixed = np.polyfit(np.array(sizes, float), t, 1, w=1.0 / t)
    return dict(fixed_s=max(float(fixed), 0.0), bytes_per_s=float(1.0 / slope))


def aead_rows():
    rows, fitted = [], {}
    for name, cls, klen in (("aes-256-gcm", AESGCM, 32),
                            ("chacha20-poly1305", ChaCha20Poly1305, 32)):
        key = os.urandom(klen)
        c = cls(key)
        nonce = os.urandom(12)
        res = {"encrypt": [], "decrypt": []}
        for n in SIZES:
            data = os.urandom(n)
            ct = c.encrypt(nonce, data, None)
            te = per_op(lambda: c.encrypt(nonce, data, None))
            td = per_op(lambda: c.decrypt(nonce, ct, None))
            res["encrypt"].append(te)
            res["decrypt"].append(td)
            rows.append(dict(cipher=name, bytes=n, encrypt_us=te * 1e6, decrypt_us=td * 1e6,
                             encrypt_mb_s=n / te / 1e6, decrypt_mb_s=n / td / 1e6))
            print(f"  {name:<18} {n:>9,} B   enc {n / te / 1e6:>8.0f} MB/s   "
                  f"dec {n / td / 1e6:>8.0f} MB/s")
        fitted[name] = {op: fit(SIZES, res[op]) for op in res}
    return rows, fitted


def handshake() -> dict:
    out = {}
    msg = os.urandom(256)                  # stands for the handshake transcript hash input
    sk = ed25519.Ed25519PrivateKey.generate()
    sig = sk.sign(msg)
    pk = sk.public_key()

    def x25519_part():
        a, b = x25519.X25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
        a.exchange(b.public_key())
        b.exchange(a.public_key())

    t_x = per_op(x25519_part)
    t_sign = per_op(lambda: sk.sign(msg))
    t_verify = per_op(lambda: pk.verify(sig, msg))
    out["x25519"] = dict(cpu_s=t_x + t_sign + t_verify, x25519_s=t_x, sign_s=t_sign,
                         verify_s=t_verify)
    print(f"  TLS 1.3 x25519 handshake CPU  {out['x25519']['cpu_s'] * 1e6:8.1f} us")
    try:
        from cryptography.hazmat.primitives.asymmetric import mlkem

        def mlkem_part():
            d = mlkem.MLKEM768PrivateKey.generate()
            ss, ct = d.public_key().encapsulate()
            d.decapsulate(ct)

        t_k = per_op(mlkem_part)
        out["x25519mlkem768"] = dict(cpu_s=out["x25519"]["cpu_s"] + t_k, mlkem768_s=t_k)
        print(f"  TLS 1.3 X25519MLKEM768 CPU    {out['x25519mlkem768']['cpu_s'] * 1e6:8.1f} us")
    except (ImportError, AttributeError, Exception) as e:     # older OpenSSL / package
        print(f"  ML-KEM not available in this cryptography/OpenSSL build ({e}); skipped")
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    print("AEAD encryption and decryption (one thread)")
    rows, fitted = aead_rows()
    print("Handshake")
    hs = handshake()
    info = dict(machine=platform.platform(), processor=platform.processor(),
                cpu_count=os.cpu_count(), python=sys.version.split()[0],
                cryptography=cryptography.__version__,
                openssl=__import__("cryptography.hazmat.backends.openssl.backend",
                                   fromlist=["backend"]).backend.openssl_version_text())
    (OUT / "crypto.json").write_text(json.dumps(
        dict(platform=info, aead=fitted, handshake=hs), indent=2), encoding="utf-8")
    with open(OUT / "crypto.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("\nFitted  t(n) = fixed + n / rate")
    for c, ops in fitted.items():
        for op, v in ops.items():
            print(f"  {c:<18} {op:<8} fixed {v['fixed_s'] * 1e6:6.2f} us   "
                  f"rate {v['bytes_per_s'] / 1e6:8.0f} MB/s")
    print(f"\nwritten to {OUT / 'crypto.json'} and {OUT / 'crypto.csv'}")
    print(f"platform: {info}")


if __name__ == "__main__":
    main()
