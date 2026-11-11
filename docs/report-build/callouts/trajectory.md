::: {.callout-note collapse="true" icon="false"}
## Analysis code: trajectory classification

Inputs: stand trajectories [`condition/outputs/data/trajectory.parquet`](../condition/outputs/data/trajectory.parquet) (one row per mapped polygon; columns poly_id, forest_type, sen_slope, sd, trajectory, c_2017..c_2025 = yearly naturalness-axis z-scores) and class cut-offs [`condition/outputs/data/trajectory_gate.json`](../condition/outputs/data/trajectory_gate.json) (key "stand" = [declining_below, improving_above], set so that 2 % of undisturbed stands fall in each tail). Full module: [`condition/src/condition/trend.py`](../condition/src/condition/trend.py) (functions calibrate_slope_gate, classify_trend). The code below, run from the repository root, reproduces the published trajectory classes (@tbl-1) exactly.

```python
import json, numpy as np, pandas as pd

t = pd.read_parquet('condition/outputs/data/trajectory.parquet')
lo, hi = json.load(open('condition/outputs/data/trajectory_gate.json'))['stand']

# sen_slope is the Theil-Sen slope of the nine yearly values; sd < 0.30 separates stable from fluctuating
c = np.where(t.sen_slope > hi, 'improving',
    np.where(t.sen_slope < lo, 'declining',
    np.where(t.sd < 0.30, 'stable', 'fluctuating')))

assert (c == t.trajectory).all()
print(pd.crosstab(t.forest_type, c))
```
:::

