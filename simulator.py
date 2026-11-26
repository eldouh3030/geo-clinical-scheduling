"""
Discrete-event simulator of query scheduling in a multi-site healthcare analytics setting.

Model
  Nodes      data nodes grouped in sites (regions) and one cloud node; each node has a
             number of query slots and an LRU cache for partitions fetched from elsewhere.
  Network    one link per site (LAN, intra-site), one link per pair of sites (WAN) and
             one link between each site and the cloud. A link serves transfers in FIFO
             order at its bandwidth; latency is added to every transfer.
  Query      arrives, waits in the scheduler, is dispatched to a node with a free slot,
             fetches the partitions it does not have (in parallel over different links),
             then runs for its processing time measured on DuckDB. The slot is held from
             dispatch to completion.

The scheduling decision itself is made by a policy object (policies.py).
"""
from __future__ import annotations

import csv
import heapq
import json
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import extensions

ARRIVAL, FINISH, WAKE = 0, 1, 2
STATIC_KEYS = {"parts", "by_holder", "total"}
FRAGMENT_ID = 1_000_000          # ids of fragments: parent id * FRAGMENT_ID + k


@dataclass
class Query:
    id: int
    arrival_unit: float
    template: str
    kind: str
    scope: str
    site: str
    sql: str
    partitions: list
    input_bytes: int
    exec_s: float
    result_bytes: int = 0
    fragments: list = field(default_factory=list)   # decomposed form of an all-regions query
    parent: "Query | None" = None
    arrival: float = 0.0
    dispatch: float = -1.0
    finish: float = -1.0
    node: str = ""
    local_bytes: int = 0
    fetched_bytes: int = 0
    extra: dict = field(default_factory=dict)   # free space for policies
    meta: dict = field(default_factory=dict)    # privacy metadata (privacy-meta.py), if any


# ------------------------------------------------------------------ loading
def load_workload(wdir: Path):
    topo = json.loads((wdir / "topology.json").read_text(encoding="utf-8"))
    parts = {}
    with open(wdir / "partitions.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["replicated"] in ("True", "true", "1"):
                continue
            parts[r["partition"]] = dict(node=r["node"], bytes=int(float(r["bytes"])))
    # Optional privacy metadata of every query and fragment, written by privacy-meta.py.
    # It is kept in a separate file so that workload.jsonl stays exactly as generated.
    privacy = {}
    pfile = wdir / "privacy.jsonl"
    if pfile.exists():
        with open(pfile, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                privacy[r["id"]] = r
    queries = []
    with open(wdir / "workload.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            pr = privacy.get(d["id"], {})
            frags = d.get("fragments", [])
            for f in frags:
                f["meta"] = pr.get("fragments", {}).get(f["site"], {})
            queries.append(Query(
                id=d["id"], arrival_unit=d["arrival_unit"], template=d["template"],
                kind=d["kind"], scope=d["scope"], site=d["site"], sql=d["sql"],
                partitions=[p for p in d["partitions"] if p in parts],
                input_bytes=d["input_bytes"], exec_s=d["exec_s"],
                result_bytes=int(d.get("result_bytes", 0)),
                fragments=frags, meta=pr.get("meta", {})))
    return topo, parts, queries


# ------------------------------------------------------------------ simulator
class Simulator:
    def __init__(self, topo: dict, parts: dict, queries: list, cfg: dict, policy, seed=0):
        self.cfg = cfg
        self.policy = policy
        self.rules = list(extensions.FEASIBILITY_RULES) + [
            extensions.RULES[n] for n in (cfg.get("rules") or [])]
        self.overheads = list(extensions.TRANSFER_OVERHEAD) + [
            extensions.OVERHEADS[n] for n in (cfg.get("overheads") or [])]
        # once-per-connection costs, e.g. a TLS 1.3 handshake (extensions.SESSION_COSTS)
        self.session_costs = list(getattr(extensions, "SESSION_SETUP", [])) + [
            extensions.SESSION_COSTS[n] for n in (cfg.get("session_costs") or [])]
        self.rng = np.random.default_rng(seed)

        self.nodes = [n["node"] for n in topo["nodes"]]
        self.site = {n["node"]: n["site"] for n in topo["nodes"]}
        self.slots = {n["node"]: int(n["slots"]) for n in topo["nodes"]}
        self.speed = {n: 1.0 for n in self.nodes}
        for n, f in (cfg.get("node_speed") or {}).items():
            self.speed[n] = float(f)
        self.data_nodes = [n for n in self.nodes if self.site[n] != "cloud"]
        self._links = {}                            # memo of link(): depends on sites only

        net = dict(topo["network"])                 # settings may override the network
        for k, v in (cfg.get("network") or {}).items():
            if isinstance(v, dict) and isinstance(net.get(k), dict):
                net[k] = {**net[k], **v}
            else:
                net[k] = v
        bps = lambda mbps: float(mbps) * 1e6 / 8.0
        self.bw = {"lan": bps(net["intra_site_mbps"]), "wan": bps(net["inter_site_mbps"]),
                   "cloud": bps(net["cloud_mbps"])}
        lat = net.get("latency_ms", {})
        self.lat = {"lan": lat.get("intra_site", 0.5) / 1000,
                    "wan": lat.get("inter_site", 20) / 1000,
                    "cloud": lat.get("cloud", 10) / 1000}

        # partitions, owners and optional replicas
        self.size = {p: v["bytes"] for p, v in parts.items()}
        self.holders = {p: [v["node"]] for p, v in parts.items()}
        for extra in cfg.get("replicas", []) or []:      # e.g. ["cloud"]
            for p in self.holders:
                if extra not in self.holders[p]:
                    self.holders[p].append(extra)
        total = sum(self.size.values())
        self.cache_cap = float(cfg.get("cache_fraction", 0.15)) * total

        # decomposition of all-regions queries (each region runs its fragment, only the
        # fragment results travel to the coordinator node)
        self.decompose = bool(cfg.get("decompose", False))
        self.coordinator = cfg.get("coordinator", "cloud")
        self.merge_s = float(cfg.get("merge_ms", 1.0)) / 1000.0

        self.queries = sorted(queries, key=lambda q: q.arrival_unit)
        for q in self.queries:
            self.prepare(q)

    def prepare(self, q: Query):
        """Precompute the partition sizes a query reads."""
        q.partitions = [p for p in q.partitions if p in self.size]
        q.extra["parts"] = [(p, self.size[p]) for p in q.partitions]
        q.extra["by_holder"] = defaultdict(int)
        for p, s in q.extra["parts"]:
            for h in self.holders[p]:
                q.extra["by_holder"][h] += s
        q.extra["total"] = sum(s for _, s in q.extra["parts"]) or 1

    def make_fragments(self, q: Query) -> list:
        kids = []
        for k, f in enumerate(q.fragments):
            c = Query(id=q.id * FRAGMENT_ID + k, arrival_unit=q.arrival_unit,
                      template=q.template, kind=q.kind, scope="fragment", site=f["site"],
                      sql=q.sql, partitions=list(f["partitions"]),
                      input_bytes=f["input_bytes"], exec_s=f["exec_s"],
                      result_bytes=int(f["result_bytes"]), parent=q,
                      meta=dict(f.get("meta") or {}))
            self.prepare(c)
            kids.append(c)
        return kids

    # ---------------------------------------------------------- constraints and costs
    def allowed(self, q: Query, node: str) -> bool:
        """True if every feasibility rule of extensions.py lets q run on node."""
        return all(rule(self, q, node) for rule in self.rules)

    def overhead(self, src: str, dst: str, nbytes: float) -> float:
        """Extra seconds for a transfer, e.g. encryption (extensions.TRANSFER_OVERHEAD)."""
        return sum(f(self, src, dst, nbytes) for f in self.overheads)

    def feasible(self, q: Query) -> bool:
        """True if at least one node may run q. The rules of extensions.py depend only on
        the query and the node, so this is decided once, when the run is set up."""
        return any(self.allowed(q, n) for n in self.nodes)

    def session_cost(self, src: str, dst: str) -> float:
        """Seconds before data can flow from src to dst, e.g. a secure-session handshake.
        Functions may look at self.sessions (pairs that already have a session)."""
        if not self.session_costs or src == dst:
            return 0.0
        return sum(f(self, src, dst) for f in self.session_costs)

    def open_session(self, src: str, dst: str):
        if self.session_costs and src != dst:
            self.sessions.add(frozenset((src, dst)))

    # ---------------------------------------------------------- helpers for policies
    def link(self, a: str, b: str):
        key = (a, b)
        hit = self._links.get(key)
        if hit is not None:
            return hit
        sa, sb = self.site[a], self.site[b]
        if sa == sb:
            hit = ("lan", sa)
        elif sa == "cloud" or sb == "cloud":
            hit = ("cloud", sb if sa == "cloud" else sa)
        else:
            hit = ("wan",) + tuple(sorted((sa, sb)))
        self._links[key] = hit
        return hit

    def is_available(self, p: str, node: str) -> bool:
        return node in self.holders[p] or p in self.cache[node]

    def local_fraction(self, q: Query, node: str) -> float:
        """Share of the input bytes already on the node (stored or cached)."""
        got = q.extra["by_holder"].get(node, 0)
        cache = self.cache[node]
        if cache:
            got += sum(s for p, s in q.extra["parts"]
                       if node not in self.holders[p] and p in cache)
        return min(1.0, got / q.extra["total"])

    def estimate_fetch(self, q: Query, node: str) -> float:
        """Estimated time until the input of q is on node (link backlog included)."""
        per_link = defaultdict(float)
        extra = defaultdict(float)
        setup = defaultdict(float)
        seen_src = set()
        lat = 0.0
        for p, s in q.extra["parts"]:
            if self.is_available(p, node):
                continue
            src = min(self.holders[p], key=lambda h: self.link_rank(h, node))
            key = self.link(src, node)
            per_link[key] += s
            if self.overheads:
                extra[key] += self.overhead(src, node, s)
            if self.session_costs and src not in seen_src:
                seen_src.add(src)
                setup[key] = max(setup[key], self.session_cost(src, node))
            lat = max(lat, self.lat[key[0]])
        if not per_link:
            return 0.0
        return lat + max(max(self.link_free[k] - self.now, setup[k]) + b / self.bw[k[0]] + extra[k]
                         for k, b in per_link.items())

    def link_rank(self, src: str, dst: str) -> float:
        key = self.link(src, dst)
        return max(self.link_free[key] - self.now, 0.0) + self.lat[key[0]] + 1.0 / self.bw[key[0]]

    def max_fetch(self, q: Query) -> float:
        """Normaliser: time to move the whole input over the slowest link class."""
        return q.extra["total"] / min(self.bw.values()) + max(self.lat.values())

    # ---------------------------------------------------------- engine
    def push(self, t, kind, payload):
        self.seq += 1
        heapq.heappush(self.events, (t, self.seq, kind, payload))

    def run(self, rate: float) -> dict:
        self.now = 0.0
        self.seq = 0
        self.events = []
        self.free = dict(self.slots)
        self.cache = {n: OrderedDict() for n in self.nodes}
        self.cache_used = {n: 0.0 for n in self.nodes}
        self.link_free = defaultdict(float)
        self.wake_at = {}
        self.bytes = defaultdict(float)          # per link class
        self.moved_once = set()                  # partitions transferred at least once
        self.redundant = 0.0
        self.result_bytes = 0.0
        self.busy = 0.0
        self.decisions = 0
        self.decision_time = 0.0
        self.done = 0
        self.waiting = 0
        self.sessions = set()                    # node pairs with an open secure session
        self.blocked = []                        # queries that no node may run

        self.units = []                           # what the scheduler actually places
        for q in self.queries:
            q.arrival = q.arrival_unit / rate
            q.dispatch = q.finish = -1.0
            q.node = ""
            q.local_bytes = q.fetched_bytes = 0
            for k in [k for k in q.extra if k not in STATIC_KEYS]:
                del q.extra[k]          # policy state from an earlier run
            # A query that no node may run is never handed to the scheduler (otherwise
            # policies that retry would wait for it forever). For a decomposed query, one
            # blocked fragment blocks the whole query and none of its fragments run.
            if self.decompose and q.fragments:
                kids = self.make_fragments(q)
                if not all(self.feasible(c) for c in kids):
                    self.blocked.append(q)
                    continue
                q.extra["pending"] = len(kids)
                q.extra["ready"] = q.arrival
                for c in kids:
                    c.arrival = q.arrival
                    self.units.append(c)
                    self.push(c.arrival, ARRIVAL, c)
            else:
                if not self.feasible(q):
                    self.blocked.append(q)
                    continue
                self.units.append(q)
                self.push(q.arrival, ARRIVAL, q)
        self.policy.reset(self)

        while self.events:
            t, _, kind, payload = heapq.heappop(self.events)
            self.now = t
            if kind == ARRIVAL:
                self.waiting += 1
                t0 = time.perf_counter()
                self.policy.on_arrival(self, payload)    # some policies decide here
                self.decision_time += time.perf_counter() - t0
            elif kind == FINISH:
                node, q = payload
                self.free[node] += 1
                self.done += 1
                self.policy.on_finish(self, q, node)
                if q.parent is not None:
                    self.fragment_done(q, node)
            elif kind == WAKE:
                if self.wake_at.get(payload) == t:
                    del self.wake_at[payload]
            self.try_dispatch()
        return self.metrics(rate)

    def try_dispatch(self):
        if self.waiting == 0:
            return
        order = [n for n in self.nodes if self.free[n] > 0]
        if not order:
            return
        k = int(self.rng.integers(len(order)))
        order = order[k:] + order[:k]
        for node in order:
            while self.free[node] > 0 and self.waiting > 0:
                t0 = time.perf_counter()
                q = self.policy.pick(self, node)
                self.decision_time += time.perf_counter() - t0
                self.decisions += 1
                if q is None:
                    retry = self.policy.retry_after(self, node)
                    if retry is not None:
                        t = self.now + retry
                        if node not in self.wake_at or self.wake_at[node] > t:
                            self.wake_at[node] = t
                            self.push(t, WAKE, node)
                    break
                self.dispatch(q, node)

    def dispatch(self, q: Query, node: str):
        self.waiting -= 1
        self.free[node] -= 1
        q.node, q.dispatch = node, self.now
        ready = self.now
        cache = self.cache[node]
        setup = {}                                  # session cost per source node
        for p, s in q.extra["parts"]:
            if node in self.holders[p]:
                q.local_bytes += s
                continue
            if p in cache:                          # cached, or being fetched already
                cache.move_to_end(p)
                ready = max(ready, cache[p])
                q.local_bytes += s
                continue
            src = min(self.holders[p], key=lambda h: self.link_rank(h, node))
            key = self.link(src, node)
            if src not in setup:
                setup[src] = self.session_cost(src, node)
            start = max(self.now + setup[src], self.link_free[key])
            end = start + s / self.bw[key[0]]
            self.link_free[key] = end
            arrive = end + self.lat[key[0]]
            if self.overheads:          # e.g. encryption at the sender, decryption here
                arrive += self.overhead(src, node, s)
            ready = max(ready, arrive)
            self.bytes[key[0]] += s
            q.fetched_bytes += s
            if p in self.moved_once:
                self.redundant += s
            self.moved_once.add(p)
            self.cache_insert(node, p, s, arrive)
        for src in setup:
            self.open_session(src, node)
        q.finish = ready + q.exec_s * self.speed[node]
        self.busy += q.finish - self.now
        self.policy.on_dispatch(self, q, node)
        self.push(q.finish, FINISH, (node, q))

    def send(self, src: str, dst: str, nbytes: float) -> float:
        """Book a transfer on the link from src to dst now; return its arrival time."""
        if src == dst or nbytes <= 0:
            return self.now
        key = self.link(src, dst)
        start = max(self.now + self.session_cost(src, dst), self.link_free[key])
        self.open_session(src, dst)
        end = start + nbytes / self.bw[key[0]]
        self.link_free[key] = end
        self.bytes[key[0]] += nbytes
        self.result_bytes += nbytes
        arrive = end + self.lat[key[0]]
        if self.overheads:
            arrive += self.overhead(src, dst, nbytes)
        return arrive

    def fragment_done(self, c: Query, node: str):
        p = c.parent
        coord = self.coordinator if self.coordinator in self.site else node
        arrive = self.send(node, coord, c.result_bytes)
        p.extra["ready"] = max(p.extra["ready"], arrive)
        p.local_bytes += c.local_bytes
        p.fetched_bytes += c.fetched_bytes
        p.dispatch = c.dispatch if p.dispatch < 0 else min(p.dispatch, c.dispatch)
        p.extra["pending"] -= 1
        if p.extra["pending"] == 0:
            p.finish = p.extra["ready"] + self.merge_s
            p.node = coord

    def cache_insert(self, node, p, s, available_at):
        if s > self.cache_cap:
            return
        cache = self.cache[node]
        while self.cache_used[node] + s > self.cache_cap and cache:
            old, _ = cache.popitem(last=False)
            self.cache_used[node] -= self.size[old]
        cache[p] = available_at
        self.cache_used[node] += s

    # ---------------------------------------------------------- metrics
    def metrics(self, rate: float) -> dict:
        qs = [q for q in self.queries if q.finish >= 0]   # queries no node may run stay out
        unfinished = len(self.queries) - len(qs)
        units = [u for u in self.units if u.finish >= 0]
        resp = np.array([q.finish - q.arrival for q in qs])
        wait = np.array([q.dispatch - q.arrival for q in qs])
        if not qs:
            raise RuntimeError("every query was blocked by the placement rules")
        first = min(q.arrival for q in qs)
        makespan = max(q.finish for q in qs) - first
        # response time per scope, so that rules that block some (often heavy) queries
        # can be compared on the queries that still run
        by_scope = {}
        for sc in ("patient", "region", "all"):
            r = [q.finish - q.arrival for q in qs if q.scope == sc]
            by_scope[f"resp_mean_{sc}_s"] = float(np.mean(r)) if r else float("nan")
        blocked = {f"blocked_{sc}": sum(q.scope == sc for q in self.blocked)
                   for sc in ("patient", "region", "all")}
        slots = sum(self.slots.values())
        inp = sum(u.extra["total"] for u in units) or 1
        moved = sum(self.bytes.values())
        return dict(
            policy=self.policy.name, rate=rate, queries=len(qs), unfinished=unfinished,
            makespan_s=makespan, throughput=len(qs) / makespan,
            resp_mean_s=resp.mean(), resp_p50_s=np.percentile(resp, 50),
            resp_p95_s=np.percentile(resp, 95), resp_p99_s=np.percentile(resp, 99),
            wait_mean_s=wait.mean(),
            moved_mb=moved / 1e6, wan_mb=self.bytes["wan"] / 1e6,
            cloud_mb=self.bytes["cloud"] / 1e6, lan_mb=self.bytes["lan"] / 1e6,
            redundant_mb=self.redundant / 1e6,
            result_mb=self.result_bytes / 1e6,
            locality=sum(u.local_bytes for u in units) / inp,
            utilization=self.busy / (slots * makespan),
            decision_us=1e6 * self.decision_time / max(self.decisions, 1),
            blocked=len(self.blocked), blocked_frac=len(self.blocked) / len(self.queries),
            **blocked, **by_scope,
            sessions=len(self.sessions),
        )
