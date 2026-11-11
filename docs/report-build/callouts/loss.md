::: {.callout-note collapse="true" icon="false"}
## Analysis code: loss detection

Inputs: [`data/c2c_pixels.geojson`](../data/c2c_pixels.geojson) (one point per 10 m cell per stand; properties forest_type, pix_id, change_year, magnitude). Full pipeline: the C2C export scripts in [`scripts/`](../scripts/). The code below, run from the repository root, reproduces the loss areas in @tbl-1 and @fig-5.

```python
import geopandas as gpd, numpy as np

# Overlapping polygons share cells, so each cell counts once per forest type
g = gpd.read_file('data/c2c_pixels.geojson').drop_duplicates(['forest_type', 'pix_id'])

# The cut-off 0.175 is the NBR drop tuned on labelled cells
loss = g[g.magnitude <= -0.175]
print(loss.groupby('forest_type').size() * 0.01)   # ha: one cell = 0.01 ha

# Change year is stamped at the end of the year before the loss (change_year X.996 = loss in year X+1)
year = np.floor(loss.change_year).astype(int) + 1
print((loss.groupby(['forest_type', year]).size() * 0.01).unstack(0))
```
:::

