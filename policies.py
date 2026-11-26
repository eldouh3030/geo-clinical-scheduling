"""
Scheduling policies. When a node has a free slot, the policy decides which waiting
query (if any) that node runs. Every policy respects the feasibility rules of
extensions.py through sim.allowed(q, node).

  hash      static: each query is bound to one allowed node by hashing its id
  fifo      a free node takes the oldest waiting query it may run
  delay     delay scheduling (Zaharia et al., EuroSys 2010): a free node looks among the
            oldest queries for one whose input is mostly local; if none, it waits up to
            a delay, then runs the oldest query
  wanaware  WAN-aware placement in the spirit of Iridium (Pu et al., SIGCOMM 2015): at
            arrival, each query is assigned to the node with the earliest estimated
            finish time (node backlog + transfer time over the actual links + processing)
  flow      min-cost assignment in the spirit of Quincy (Isard et al., SOSP 2009): at every
            scheduling instant the waiting queries are matched to free slots by solving an
            assignment problem; running a query costs its estimated transfer time, leaving
            it unscheduled costs gamma x its waiting time
  score     the proposed policy: a free node takes the query with the best score, which
            combines locality, estimated transfer time (link backlog included) and waiting
            time; it waits for a local query at most delay_s, like delay scheduling, but
            when the delay expires it takes the best-scored query instead of the oldest
"""
from __future__ import annotations

import math
import zlib
from collections import deque

import numpy as np

import extensions

try:
    from scipy.optimize import linear_sum_assignment
except ImportError:  # only needed by the flow policy
    linear_sum_assignment = None


class Policy:
    name = "base"

    def __init__(self, **params):
        self.params = params

    def reset(self, sim):
        pass

    def on_arrival(self, sim, q):
        raise NotImplementedError

    def pick(self, sim, node):
        raise NotImplementedError

    def on_dispatch(self, sim, q, node):
        pass

    def on_finish(self, sim, q, node):
        pass

    def retry_after(self, sim, node):
        return None


# ------------------------------------------------------------------ baselines
class HashPolicy(Policy):
    name = "hash"

    def reset(self, sim):
        self.queues = {n: deque() for n in sim.nodes}

    def on_arrival(self, sim, q):
        ok = [n for n in sim.nodes if sim.allowed(q, n)]
        if ok:                      # a query no node may run is never scheduled
            self.queues[ok[zlib.crc32(str(q.id).encode()) % len(ok)]].append(q)

    def pick(self, sim, node):
        return self.queues[node].popleft() if self.queues[node] else None


class FifoPolicy(Policy):
    name = "fifo"

    def reset(self, sim):
        self.queue = []

    def on_arrival(self, sim, q):
        self.queue.append(q)

    def pick(self, sim, node):
        for i, q in enumerate(self.queue):
            if sim.allowed(q, node):
                return self.queue.pop(i)
        return None


class DelayPolicy(Policy):
    """Delay scheduling with a time-based delay and a scan window."""
    name = "delay"

    def reset(self, sim):
        self.queue = []
        self.window = int(self.params.get("window", 16))
        self.threshold = float(self.params.get("local_threshold", 0.8))
        self.delay = float(self.params.get("delay_s", 0.5))

    def on_arrival(self, sim, q):
        self.queue.append(q)

    def pick(self, sim, node):
        best, best_f, first = None, -1.0, None
        seen = 0
        for i, q in enumerate(self.queue):
            if seen >= self.window:
                break
            if not sim.allowed(q, node):
                continue
            seen += 1
            if first is None:
                first = i
            f = sim.local_fraction(q, node)
            if f > best_f:
                best, best_f = i, f
            if f >= 1.0:
                break
        if first is None:
            return None
        if best_f >= self.threshold:
            return self.queue.pop(best)
        if sim.now - self.queue[first].arrival >= self.delay:
            return self.queue.pop(first)
        return None

    def retry_after(self, sim, node):
        if not self.queue:
            return None
        return max(self.delay - (sim.now - self.queue[0].arrival), 1e-3)


class WanAwarePolicy(Policy):
    """Earliest estimated finish time at arrival, with per-node queues."""
    name = "wanaware"

    def reset(self, sim):
        self.queues = {n: deque() for n in sim.nodes}
        self.ready = {n: 0.0 for n in sim.nodes}     # estimated time a slot frees up

    def on_arrival(self, sim, q):
        best, best_t = None, math.inf
        for n in sim.nodes:
            if not sim.allowed(q, n):
                continue
            start = max(sim.now, self.ready[n])
            t = start + sim.estimate_fetch(q, n) + q.exec_s * sim.speed[n]
            if t < best_t:
                best, best_t = n, t
        if best is None:            # no node may run it: never scheduled
            return
        start = max(sim.now, self.ready[best])
        self.ready[best] = start + (best_t - start) / sim.slots[best]
        self.queues[best].append(q)

    def pick(self, sim, node):
        return self.queues[node].popleft() if self.queues[node] else None


class FlowPolicy(Policy):
    """Quincy-style min-cost matching of waiting queries to free slots."""
    name = "flow"

    def reset(self, sim):
        if linear_sum_assignment is None:
            raise SystemExit("the flow policy needs scipy: pip install scipy")
        self.queue = []
        self.window = int(self.params.get("window", 64))
        self.gamma = float(self.params.get("gamma", 1.0))
        self.retry = float(self.params.get("retry_s", 0.05))
        self.plan_time = -1.0
        self.plan = {}

    def on_arrival(self, sim, q):
        self.queue.append(q)
        self.plan_time = -1.0

    def on_finish(self, sim, q, node):
        self.plan_time = -1.0

    def make_plan(self, sim):
        self.plan_time = sim.now
        self.plan = {}
        rows = self.queue[: self.window]
        cols = [n for n in sim.nodes for _ in range(sim.free[n])]
        if not rows or not cols:
            return
        big = 1e9
        cost = np.full((len(rows), len(cols) + len(rows)), big)
        for i, q in enumerate(rows):
            per_node = {}
            for n in set(cols):
                if sim.allowed(q, n):
                    per_node[n] = sim.estimate_fetch(q, n)
            for j, n in enumerate(cols):
                if n in per_node:
                    cost[i, j] = per_node[n]
            wait = sim.now - q.arrival
            cost[i, len(cols):] = self.gamma * wait + 1e-6   # leave unscheduled
        r, c = linear_sum_assignment(cost)
        for i, j in zip(r, c):
            if j < len(cols) and cost[i, j] < big:
                self.plan.setdefault(cols[j], []).append(rows[i])

    def pick(self, sim, node):
        if self.plan_time != sim.now:
            self.make_plan(sim)
        lst = self.plan.get(node)
        while lst:
            q = lst.pop(0)
            if q in self.queue:
                self.queue.remove(q)
                return q
        return None

    def on_dispatch(self, sim, q, node):
        pass

    def retry_after(self, sim, node):
        return self.retry if self.queue else None


# ------------------------------------------------------------------ proposed policy
class ScorePolicy(Policy):
    """Score-based selection with bounded waiting."""
    name = "score"

    def reset(self, sim):
        p = self.params
        self.queue = []
        self.window = int(p.get("window", 16))
        self.threshold = float(p.get("local_threshold", 0.8))
        self.delay = float(p.get("delay_s", 0.5))
        self.w = dict(locality=1.0, transfer=0.5, age=0.2)
        self.w.update(p.get("weights", {}) or {})
        self.terms = list(getattr(extensions, "SCORE_TERMS", []))

    def on_arrival(self, sim, q):
        self.queue.append(q)

    def score(self, sim, q, node):
        loc = sim.local_fraction(q, node)
        fetch = sim.estimate_fetch(q, node) if loc < 1.0 else 0.0
        transfer = min(1.0, fetch / sim.max_fetch(q))
        age = min(1.0, (sim.now - q.arrival) / self.delay) if self.delay > 0 else 1.0
        s = self.w["locality"] * loc - self.w["transfer"] * transfer + self.w["age"] * age
        for weight, term in self.terms:
            s += weight * term(sim, q, node)
        return s, loc

    def pick(self, sim, node):
        best, best_s, best_loc = None, -math.inf, 0.0
        seen, first = 0, None
        for i, q in enumerate(self.queue):
            if seen >= self.window:
                break
            if not sim.allowed(q, node):
                continue
            seen += 1
            if first is None:
                first = i
            s, loc = self.score(sim, q, node)
            if s > best_s:
                best, best_s, best_loc = i, s, loc
        if best is None:
            return None
        if best_loc >= self.threshold or sim.now - self.queue[first].arrival >= self.delay:
            return self.queue.pop(best)
        return None

    def retry_after(self, sim, node):
        if not self.queue:
            return None
        return max(self.delay - (sim.now - self.queue[0].arrival), 1e-3)


POLICIES = {c.name: c for c in (HashPolicy, FifoPolicy, DelayPolicy, WanAwarePolicy,
                                FlowPolicy, ScorePolicy)}


def make_policy(spec: dict) -> Policy:
    spec = dict(spec)
    kind = spec.pop("policy")
    label = spec.pop("label", kind)
    p = POLICIES[kind](**spec)
    p.name = label
    return p
