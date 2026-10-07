"""Do the process scores predict success? Fit them to the outcomes somebody stated, and say.

The seven process scores (analysis.components) and the weights of their overall were chosen by hand. Each is a
fair coaching hint; whether one says anything about whether a task worked is a question for data. Here:

  * the data: tasks whose outcome somebody stated -- graded, or a feedback score -- never the inferred ones,
    which would fit the scores to the guess. Each task's scores are computed without the terms that restate its
    outcome (a failure's -40 on reliability, a rework's -35 on autonomy), or the fit would be circular;
  * for each score alone, its AUC: the chance it ranks a task that worked above one that didn't (0.5 is a coin);
  * a logistic regression of success on all of them (standardized, lightly regularized), and its AUC out of
    sample (5-fold), against the AUC of the overall score with the default weights on the same tasks;
  * fitted weights for the overall score: each score's coefficient on its own scale, for the scores that also
    predict success alone (AUC at least MIN_AUC) -- the rest get no say; a score too rare in the data to judge
    keeps its default weight.

`[scores] weights = "fitted"` puts the fitted weights in use when they predict stated outcomes better than the
defaults by at least ADOPT_MARGIN out of sample, on at least MIN_TASKS tasks with MIN_CLASS of each outcome. The
engine refits daily; what is in use is shown on the Process Review page beside this report. Pure Python.
"""
import math

from .analysis import SCORE_WEIGHTS, components, overall

NAMES = tuple(SCORE_WEIGHTS)
STATED = ("graded", "feedback")
MIN_TASKS, MIN_CLASS = 50, 10
MAX_TASKS = 3000
ADOPT_MARGIN = 0.02
MIN_AUC = 0.55          # what a score must predict alone to get any fitted weight
FOLDS = 5


def dataset(tasks):
    """(feature dicts, labels) from the tasks with a stated outcome that were scored."""
    xs, ys = [], []
    stated = [t for t in tasks if t.get("outcome_source") in STATED and t.get("score") is not None]
    stated.sort(key=lambda t: (t.get("started") or 0, t.get("id") or ""))
    for t in stated[-MAX_TASKS:]:                       # the most recent: what the agent is like now
        xs.append(components(t, t.get("cost_vs_baseline") or 1, outcome=False))
        ys.append(1 if t.get("outcome") == "completed" else 0)
    return xs, ys


def auc(scores, labels):
    """Area under the ROC curve (Mann-Whitney, ties averaged); None without both classes."""
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    r = sum(ranks[i] for i in range(len(labels)) if labels[i])
    return round((r - pos * (pos + 1) / 2) / (pos * neg), 4)


def _matrix(xs, means, sds):
    return [[((x[n] if x[n] is not None else means[n]) - means[n]) / sds[n] for n in NAMES] for x in xs]


def _standardize(xs):
    means, sds = {}, {}
    for n in NAMES:
        v = [x[n] for x in xs if x[n] is not None]
        m = sum(v) / len(v) if v else 0.0
        sd = math.sqrt(sum((a - m) ** 2 for a in v) / len(v)) if v else 0.0
        means[n], sds[n] = m, sd if sd > 1e-9 else 1.0
    return means, sds


def _solve(A, b):
    """A x = b by Gaussian elimination with partial pivoting (A small and, with the ridge, well conditioned)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b_ for a, b_ in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def logistic(X, y, l2=1.0, iters=25):
    """Ridge-regularized logistic regression by Newton's method (IRLS). Returns (intercept, coefficients).
    The intercept isn't penalized. Deterministic, and a few iterations converge."""
    d = len(X[0]) if X else 0
    Z = [[1.0] + list(xi) for xi in X]
    w = [0.0] * (d + 1)
    for _ in range(iters):
        H = [[0.0] * (d + 1) for _ in range(d + 1)]
        g = [0.0] * (d + 1)
        for zi, yi in zip(Z, y):
            t = sum(a * b for a, b in zip(w, zi))
            p = 1 / (1 + math.exp(-max(-30.0, min(30.0, t))))
            r, v = yi - p, p * (1 - p)
            for j in range(d + 1):
                g[j] += r * zi[j]
                vz = v * zi[j]
                row = H[j]
                for k in range(j, d + 1):
                    row[k] += vz * zi[k]
        for j in range(d + 1):
            for k in range(j):
                H[j][k] = H[k][j]
            if j:
                H[j][j] += l2
                g[j] -= l2 * w[j]
        step = _solve(H, g)
        w = [a + b for a, b in zip(w, step)]
        if max(abs(x) for x in step) < 1e-8:
            break
    return w[0], w[1:]


def _predict(w0, w, X):
    return [w0 + sum(a * b for a, b in zip(w, xi)) for xi in X]


def fit(tasks):
    """The report: n, the AUC of each score and of the default and fitted overall, fitted weights, and whether
    they should be adopted."""
    xs, ys = dataset(tasks)
    n, pos = len(ys), sum(ys)
    rep = {"n": n, "worked": pos, "failed": n - pos, "auc_default": None, "auc_fitted": None, "weights": None,
           "adopt": False, "components": [], "enough": n >= MIN_TASKS and min(pos, n - pos) >= MIN_CLASS}
    default = [overall(x) for x in xs]
    rep["auc_default"] = auc([d if d is not None else 0.0 for d in default], ys)
    for name in NAMES:
        have = [(x[name], y) for x, y in zip(xs, ys) if x[name] is not None]
        rep["components"].append({"name": name, "tasks": len(have), "default_weight": SCORE_WEIGHTS[name],
                                  "auc": auc([a for a, _ in have], [b for _, b in have]) if have else None})
    if not rep["enough"]:
        return rep
    # out of sample: each task predicted by a model that never saw it (folds by position, deterministic)
    oof = [0.0] * n
    for f in range(FOLDS):
        train = [i for i in range(n) if i % FOLDS != f]
        test = [i for i in range(n) if i % FOLDS == f]
        means, sds = _standardize([xs[i] for i in train])
        w0, w = logistic(_matrix([xs[i] for i in train], means, sds), [ys[i] for i in train])
        for i, p in zip(test, _predict(w0, w, _matrix([xs[i] for i in test], means, sds))):
            oof[i] = p
    rep["auc_fitted"] = auc(oof, ys)
    means, sds = _standardize(xs)
    w0, w = logistic(_matrix(xs, means, sds), ys)
    # a score the fit had too little of keeps its default weight: no evidence isn't evidence it doesn't predict
    seen = {c["name"] for c in rep["components"] if c["tasks"] >= 2 * MIN_CLASS}
    # weight only for a score that predicts success on its own too: next to a strong predictor, noise picks up
    # a coefficient by chance (seen at n = 3000), and a score has to earn its say
    alone = {c["name"]: c["auc"] or 0.5 for c in rep["components"]}
    raw = {name: max(0.0, w[j] / sds[name]) if alone[name] >= MIN_AUC else 0.0
           for j, name in enumerate(NAMES) if name in seen}
    total, kept = sum(raw.values()), {name: SCORE_WEIGHTS[name] for name in NAMES if name not in seen}
    if total > 0:
        share = 1 - sum(kept.values())
        rep["weights"] = {name: round(raw[name] / total * share, 4) if name in raw else kept[name] for name in NAMES}
    for c, j in zip(rep["components"], range(len(NAMES))):
        c["coefficient"] = round(w[j], 4)
        c["fitted_weight"] = (rep["weights"] or {}).get(c["name"])
    rep["adopt"] = bool(rep["weights"]) and rep["auc_fitted"] is not None and rep["auc_default"] is not None \
        and rep["auc_fitted"] >= rep["auc_default"] + ADOPT_MARGIN
    return rep
