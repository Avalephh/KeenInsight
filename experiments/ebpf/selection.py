"""Offline standardized Lasso; labels must come from real, independent trials."""
import hashlib
import json
import math
from pathlib import Path

from .catalog import BY_NAME


def fit(rows, alpha=.05, iterations=10000, tolerance=1e-9):
    if len(rows) < 3 or not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("at least three training trials and alpha > 0 are required")
    identities = [str(r["trial_id"]) for r in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("trial_id must identify independent trials, not duplicate windows")
    names = sorted({name for row in rows for name in row["features"]})
    if not names or set(names) - set(BY_NAME):
        raise ValueError("features must be probe names from the allowlist")
    if any(set(row["features"]) != set(names) for row in rows):
        raise ValueError("missing features are unknown, not zeros; use a common feature set")
    x = [[float(row["features"][n]) for n in names] for row in rows]
    y = [float(row["degradation"]) for row in rows]
    if not all(math.isfinite(v) for row in x for v in row) or not all(map(math.isfinite, y)):
        raise ValueError("training measurements must be finite")
    count = len(rows)
    means = [sum(row[j] for row in x) / count for j in range(len(names))]
    scales = [math.sqrt(sum((row[j] - means[j]) ** 2 for row in x) / count)
              for j in range(len(names))]
    z = [[(row[j] - means[j]) / scales[j] if scales[j] else 0.0
          for j in range(len(names))] for row in x]
    intercept = sum(y) / count
    residual = [v - intercept for v in y]
    weights = [0.0] * len(names)
    converged = False
    for step in range(iterations):
        change = 0.0
        for j in range(len(names)):
            if not scales[j]:
                continue
            old = weights[j]
            rho = sum(z[i][j] * (residual[i] + z[i][j] * old)
                      for i in range(count)) / count
            new = math.copysign(max(0.0, abs(rho) - alpha), rho)
            for i in range(count):
                residual[i] += z[i][j] * (old - new)
            weights[j] = new
            change = max(change, abs(old - new))
        if change < tolerance:
            converged = True
            break
    if not converged:
        raise ValueError("Lasso did not converge; increase iterations/check correlated features")
    return {"schema": "keeninsight.lasso.v1", "alpha": alpha,
            "objective": "squared_error/(2*n) + alpha*L1; standardized X, centered y",
            "coefficients": dict(zip(names, weights)), "means": dict(zip(names, means)),
            "scales": dict(zip(names, scales)), "intercept": intercept,
            "training_trial_ids": identities, "iterations": step + 1,
            "validation": "training_only; held-out effectiveness is not established"}


def train_file(source, destination, alpha=.05):
    raw = Path(source).read_bytes()
    model = fit([json.loads(line) for line in raw.splitlines() if line.strip()], alpha)
    model["training_sha256"] = hashlib.sha256(raw).hexdigest()
    with Path(destination).open("x") as handle:
        json.dump(model, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return model
