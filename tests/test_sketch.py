"""The quantile sketch behind percentiles across rolled-up days (sketch.py): within 1% relative, and mergeable."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics.analysis import pct  # noqa: E402
from agentdynamics.sketch import ALPHA, Sketch  # noqa: E402


def exact(values, q):
    v = sorted(values)
    return v[int(q * (len(v) - 1))]


class SketchTest(unittest.TestCase):
    def test_quantiles_are_within_one_percent(self):
        rng = random.Random(5)
        for values in ([rng.lognormvariate(-5, 1.5) for _ in range(5000)],     # costs: dollars, long tail
                       [rng.expovariate(1 / 40) for _ in range(5000)],           # seconds
                       [0.0] * 50 + [rng.uniform(1, 2) for _ in range(500)]):   # with zeros
            sk = Sketch.of(values)
            for q in (0.01, 0.25, 0.5, 0.9, 0.95, 0.99):
                want = exact(values, q)
                got = sk.quantile(q)
                self.assertLessEqual(abs(got - want), ALPHA * want + 1e-12, f"q={q}")

    def test_merging_is_the_sketch_of_the_union(self):
        rng = random.Random(9)
        a = [rng.lognormvariate(0, 1) for _ in range(800)] + [0.0] * 40
        b = [rng.lognormvariate(2, 1) for _ in range(300)] + [0.0] * 500
        merged = Sketch.of(a).merge(Sketch.of(b))
        whole = Sketch.of(a + b)
        self.assertEqual((merged.bins, merged.zeros, merged.count), (whole.bins, whole.zeros, whole.count))
        self.assertLess(abs(merged.quantile(0.5) - pct(a + b, 0.5)) / pct(a + b, 0.5), 0.02)

    def test_json_round_trip_and_empty(self):
        sk = Sketch.of([0.0, 0.5, 1.5, 1.5, 300.0])
        back = Sketch.from_json(sk.to_json())
        self.assertEqual((back.bins, back.zeros, back.count), (sk.bins, sk.zeros, sk.count))
        self.assertIsNone(Sketch().quantile(0.5))
        self.assertEqual(Sketch.from_json(None).count, 0)
        self.assertEqual(Sketch.of([0.0, 0.0, 5.0]).quantile(0.5), 0.0)


if __name__ == "__main__":
    unittest.main()
