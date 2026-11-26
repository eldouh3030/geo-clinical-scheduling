"""
Extension points for security- and privacy-aware scheduling.

FEASIBILITY_RULES   list of functions(sim, q, node) -> bool. A query (or a fragment of a
                    decomposed query) may run on a node only if every rule returns True.
                    The simulator applies the rules to every policy, so all policies are
                    compared under the same constraints. A query that no node may run is
                    not scheduled at all and is counted in the `blocked` and `unfinished`
                    columns; if one fragment of a decomposed query is blocked, the whole
                    query is blocked.

TRANSFER_OVERHEAD   list of functions(sim, src, dst, nbytes) -> seconds. Extra time added
                    to every transfer from node src to node dst, for every policy.
                    Example: encryption and decryption time of nbytes at a measured rate.

SESSION_SETUP       list of functions(sim, src, dst) -> seconds. Time before data can
                    flow between two nodes, e.g. a TLS 1.3 handshake. sim.sessions holds
                    the node pairs that already have an open session in this run.

RULES, OVERHEADS,   named libraries. An experiment switches entries on by name in its
SESSION_COSTS       configuration, e.g.
                      simulator: {rules: [residency], overheads: [aes-gcm],
                                  session_costs: [tls13]}

Useful information inside a function:
  sim.site[node]            site (region) of a node, or "cloud"
  sim.nodes                 all node names
  sim.holders[p]            nodes that store partition p (the first one is its home)
  sim.link(a, b)            link class of a transfer: ("lan", ...), ("wan", ...), ("cloud", ...)
  sim.lat[cls]              one-way latency of a link class in seconds
  q.extra["parts"]          list of (partition, bytes) that q reads
  q.scope                   "patient", "region", "all", or "fragment" (one region's part
                            of a decomposed all-regions query)
  q.site                    region of q ("all" for an undecomposed all-regions query)
  q.template, q.sql         which repository query it comes from, and its SQL text
  q.result_bytes            size of q's result
  q.meta                    privacy metadata of q's result (privacy-meta.py): identifiable,
                            result_class, min_group, rows
  sim.local_fraction(q, n)  share of q's input already on node n
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CRYPTO_FILE = ROOT / "measurements" / "crypto.json"


# ================================================================ placement rules
def home_sites(sim, q) -> set:
    """Regions whose raw patient data q reads (cached per run)."""
    if "home_sites" not in q.extra:
        q.extra["home_sites"] = {sim.site[sim.holders[p][0]] for p, _ in q.extra["parts"]}
    return q.extra["home_sites"]


def residency(sim, q, node) -> bool:
    """Data residency: raw patient data never leaves its region.
    A query may run only on a node of the region that holds all the raw data it reads.
    Queries that read several regions can only run when decomposed into fragments.
    Results (aggregates) may still travel, e.g. fragment results to the coordinator."""
    sites = home_sites(sim, q)
    return not sites or (len(sites) == 1 and sim.site[node] in sites)


def no_raw_on_cloud(sim, q, node) -> bool:
    """Trust levels: the cloud coordinator is honest-but-curious and never processes raw
    patient data. Implied by residency; it matters only without the residency rule."""
    return sim.site[node] != "cloud" or not q.extra["parts"]


def _meta(q) -> dict:
    if not q.meta:
        raise RuntimeError(
            f"query {q.id} has no privacy metadata; run  python privacy-meta.py <workloads>  "
            "before using a result-release rule")
    return q.meta


def no_identifiable_results(sim, q, node) -> bool:
    """No identifiable results across regions. A fragment result leaves its region (it is
    sent to the coordinator), so a fragment whose result has a patient identifier column
    (patientunitstayid, uniquepid, patienthealthsystemstayid) may run nowhere. Such
    all-region queries cannot be federated and are blocked."""
    if q.scope != "fragment":
        return True            # results of other queries do not leave their region
    return not _meta(q)["identifiable"]


def min_group(k: int, strict: bool = True):
    """Minimum aggregation size (small-cell rule). A fragment result may leave its region
    only if every output row aggregates at least k patients.
      min_group from privacy-meta.py: 1 for identifiable or row-level results; the
      smallest number of distinct patients per group when it can be computed
      (group_basis "patients"); otherwise the smallest count in a count column
      (group_basis "rows", an upper bound on patients); unknown if neither exists.
    strict=True accepts only group sizes counted in patients and blocks the rest;
    strict=False also accepts row counts and unknown sizes. The two bound the result."""
    def rule(sim, q, node) -> bool:
        if q.scope != "fragment":
            return True
        m = _meta(q)
        if m["result_class"] in ("empty", "zero"):
            return True        # nothing about any patient is released
        if m["min_group"] is None or (strict and m.get("group_basis") != "patients"):
            return not strict
        return m["min_group"] >= k
    rule.__name__ = f"min_group_{k}{'' if strict else '_lenient'}"
    return rule


# ================================================================ security costs
_CRYPTO = None


def crypto() -> dict:
    """Rates measured on the evaluation machine by measure-crypto.py."""
    global _CRYPTO
    if _CRYPTO is None:
        if not CRYPTO_FILE.exists():
            raise RuntimeError(f"missing {CRYPTO_FILE}; run  python measure-crypto.py  first")
        _CRYPTO = json.loads(CRYPTO_FILE.read_text(encoding="utf-8"))
    return _CRYPTO


# Threat model: the WAN and the site-cloud links are untrusted; the LAN inside a regional
# data centre is trusted. The "-all" variants also protect the LAN (zero trust).
UNTRUSTED = {"wan", "cloud"}
ALL_LINKS = {"lan", "wan", "cloud"}


def aead(cipher: str, classes: set):
    """Encryption at the sender and decryption at the receiver of every transfer on a
    protected link: t(n) = fixed + n / rate for each side, fitted to measurements."""
    k = {}                                  # constants, read once from crypto.json

    def cost(sim, src, dst, nbytes) -> float:
        if sim.link(src, dst)[0] not in classes:
            return 0.0
        if not k:
            c = crypto()["aead"][cipher]
            e, d = c["encrypt"], c["decrypt"]
            k["fixed"] = e["fixed_s"] + d["fixed_s"]
            k["per_byte"] = 1.0 / e["bytes_per_s"] + 1.0 / d["bytes_per_s"]
        return k["fixed"] + nbytes * k["per_byte"]
    cost.__name__ = f"{cipher}_{'all' if classes == ALL_LINKS else 'untrusted'}"
    return cost


def tls13(kex: str = "x25519", classes: set = UNTRUSTED, reuse: bool = True):
    """TLS 1.3 full handshake (RFC 8446): one round trip before application data, plus the
    measured CPU time of the key exchange and of the certificate signature (both sides).
    reuse=True: one session per node pair, kept for the whole run (persistent connections).
    reuse=False: a new handshake for every transfer (stress case, no connection reuse)."""
    def cost(sim, src, dst) -> float:
        cls = sim.link(src, dst)[0]
        if cls not in classes:
            return 0.0
        if reuse and frozenset((src, dst)) in sim.sessions:
            return 0.0
        return 2.0 * sim.lat[cls] + crypto()["handshake"][kex]["cpu_s"]
    cost.__name__ = f"tls13_{kex}{'' if reuse else '_per_transfer'}"
    return cost


# ================================================================ registries
RULES = {
    "residency": residency,
    "no-raw-on-cloud": no_raw_on_cloud,
    "no-identifiable-results": no_identifiable_results,
}
for _k in (5, 10, 20):
    RULES[f"min-group-{_k}"] = min_group(_k, strict=True)
    RULES[f"min-group-{_k}-lenient"] = min_group(_k, strict=False)

OVERHEADS = {
    "aes-gcm": aead("aes-256-gcm", UNTRUSTED),
    "aes-gcm-all": aead("aes-256-gcm", ALL_LINKS),
    "chacha20": aead("chacha20-poly1305", UNTRUSTED),
    "chacha20-all": aead("chacha20-poly1305", ALL_LINKS),
}

SESSION_COSTS = {
    "tls13": tls13("x25519"),
    "tls13-all": tls13("x25519", ALL_LINKS),
    "tls13-mlkem": tls13("x25519mlkem768"),          # hybrid post-quantum key exchange
    "tls13-per-transfer": tls13("x25519", reuse=False),
}

FEASIBILITY_RULES: list = []      # always on (empty in the base study)
TRANSFER_OVERHEAD: list = []      # always on (empty in the base study)
SESSION_SETUP: list = []          # always on (empty in the base study)
