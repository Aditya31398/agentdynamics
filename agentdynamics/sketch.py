"""A mergeable quantile sketch (DDSketch, Masson et al., VLDB 2019) for percentiles across daily rollups.

Retention keeps each day's totals and drops its tasks (store.freeze_days). A sum adds across days; a median
doesn't. So each rollup row also keeps a sketch of its tasks' cost, wall clock and agent time: values bucketed on
a logarithmic scale, where any quantile read back is within `ALPHA` (1%) of the true value, relative. Two
sketches merge by adding their bucket counts, so a median or p95 over 90 days of rollups and the tasks still held
is one merge away, and costs a few hundred buckets per row whatever the traffic.

Stored as compact JSON: {"z": count of zeros (and values too small to bucket), "b": {index: count}}.
"""
import json
import math

ALPHA = 0.01
GAMMA = (1 + ALPHA) / (1 - ALPHA)
LOG_GAMMA = math.log(GAMMA)
MIN_VALUE = 1e-9            # USD and seconds: anything smaller counts as zero


class Sketch:
    __slots__ = ("zeros", "bins", "count")

    def __init__(self, zeros=0, bins=None):
        self.zeros, self.bins = zeros, dict(bins or {})
        self.count = zeros + sum(self.bins.values())

    def add(self, v):
        if v is None:
            return
        if v <= MIN_VALUE:
            self.zeros += 1
        else:
            i = math.ceil(math.log(v) / LOG_GAMMA)
            self.bins[i] = self.bins.get(i, 0) + 1
        self.count += 1

    def merge(self, other):
        self.zeros += other.zeros
        for i, n in other.bins.items():
            self.bins[i] = self.bins.get(i, 0) + n
        self.count += other.count
        return self

    def quantile(self, q):
        """The q-quantile (0..1), within ALPHA relative; None when empty."""
        if not self.count:
            return None
        rank = q * (self.count - 1)
        if rank < self.zeros:
            return 0.0
        seen = self.zeros
        for i in sorted(self.bins):
            seen += self.bins[i]
            if seen > rank:
                return 2 * GAMMA ** i / (GAMMA + 1)      # the bucket's midpoint in relative terms
        return 2 * GAMMA ** max(self.bins) / (GAMMA + 1)

    def to_json(self):
        return json.dumps({"z": self.zeros, "b": {str(i): n for i, n in sorted(self.bins.items())}}, separators=(",", ":"))

    @classmethod
    def from_json(cls, s):
        if not s:
            return cls()
        d = json.loads(s) if isinstance(s, str) else s
        return cls(d.get("z", 0), {int(i): n for i, n in (d.get("b") or {}).items()})

    @classmethod
    def of(cls, values):
        sk = cls()
        for v in values:
            sk.add(v)
        return sk
