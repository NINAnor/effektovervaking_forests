"""Reference pools: what "good" and "bad" mean for these two forest types, when.

* Good pool: `god`-labelled stands of the same forest type, as local (nearest
  donors) and global (all donors) scopes. Never the scored stand, anything
  overlapping it, or any donor within `EXCLUDE_KM_DEFAULT` of its centroid (the
  transformed annulus's inner radius; one rule for both pools).
* A `tilstand` label describes the survey date, so a donor contributes only its
  survey-year pixels (`anchor_year`); every scored row carries `label_age`.
* Bad pool: WorldCover transformed pixels (`condition.worldcover`).
* The unit is the pixel, which belongs to a set of polygons, so pools are built
  and excluded by `pixel_id` (`condition.membership`); `conflict` pixels enter
  no pool.

See METHODS.md section 3.
"""

from __future__ import annotations

import warnings
import zlib

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.paths import ANNULUS_INNER_M, BANDS

# Inner radius of the good-pool exclusion: the same 3 km as the transformed
# pool's annulus (METHODS.md, E20).
EXCLUDE_KM_DEFAULT = ANNULUS_INNER_M / 1000.0

__all__ = [
    "TILSTAND_ROLE",
    "EXCLUDE_KM_DEFAULT",
    "assign_roles",
    "survey_year",
    "anchor_donors",
    "PoolBuilder",
]

# `tilstand` as it appears in the GeoPackage (Norwegian, camelCase for the
# two-word values) -> the role that value plays in scoring.
TILSTAND_ROLE = {
    "god": "good",
    "moderat": "holdout",
    "dårlig": "bad",
    "sværtRedusert": "bad",
}


def assign_roles(polygons: pd.DataFrame) -> pd.Series:
    """Map each polygon's `tilstand` to good / bad / holdout / unlabelled."""
    if "tilstand" not in polygons.columns:
        return pd.Series("unlabelled", index=polygons.index, dtype=object)
    return polygons["tilstand"].map(TILSTAND_ROLE).fillna("unlabelled").astype(object)


def survey_year(polygons: pd.DataFrame) -> pd.Series:
    """When each stand was actually visited -- the only year its label describes.

    Prefers the `Kartlagtda` date, else the `year` column (typed inconsistently
    across layers, so coerced). Nullable Int64; NA means the stand cannot be a donor.
    """
    yr = pd.to_numeric(polygons.get("year"), errors="coerce")
    if "Kartlagtda" in polygons.columns:
        d = pd.to_datetime(polygons["Kartlagtda"], errors="coerce", format="mixed")
        yr = d.dt.year.fillna(yr)
    return yr.round().astype("Int64")


def anchor_donors(
    polygons: pd.DataFrame,
    pixel_roles: pd.DataFrame,
    membership: pd.DataFrame,
    aef_years: tuple[int, ...],
    min_pixels: int = 5,
) -> pd.DataFrame:
    """The god-labelled stands eligible to be references, and in which year.

    A stand qualifies if its survey year is inside the AlphaEarth record, and it
    still owns at least `min_pixels` unambiguously `good` pixels (a `conflict`
    pixel counts for nobody) that exist in the anchor year.
    Returns `poly_id, forest_type, anchor_year, n_good_pixels`.
    """
    good_px = pixel_roles.loc[pixel_roles["role"] == "good", ["pixel_id"]]
    owned = membership.merge(good_px, on="pixel_id")
    counts = owned.groupby("poly_id").size().rename("n_good_pixels")

    d = polygons[["poly_id", "forest_type", "ref_role"]].copy()
    d["anchor_year"] = survey_year(polygons)
    d = d[d["ref_role"] == "good"]
    d = d[d["anchor_year"].isin(aef_years)]
    d = d.merge(counts, on="poly_id", how="inner")
    d = d[d["n_good_pixels"] >= min_pixels]
    return d[["poly_id", "forest_type", "anchor_year", "n_good_pixels"]].reset_index(
        drop=True
    )


class PoolBuilder:
    """Builds the reference matrices a probe needs, with the leaks closed.

    Embeddings are held as dense float32 arrays indexed by pixel, so a lookup is
    an integer take. `__init__` decides once the pixel rows each donor
    contributes (at its anchor year), each stand's transformed pixels, and the
    stands too close to be its reference.
    """

    def __init__(
        self,
        pixels: pd.DataFrame,
        bad_pixels: pd.DataFrame,
        donors: pd.DataFrame,
        membership: pd.DataFrame,
        pixel_roles: pd.DataFrame,
        bad_membership: pd.DataFrame,
        neighbours: pd.DataFrame,
        centroids: pd.DataFrame,
        poly_type: pd.Series | None = None,
        blocks: pd.Series | None = None,
        exclude_km: float = EXCLUDE_KM_DEFAULT,
        exclude_block: bool = False,
        n_donors: int = 8,
        donor_km: float = 0.0,
        pool_by_type: dict | None = None,
        max_km: float = 50.0,
        max_per_donor: int = 60,
        max_global: int = 2000,
        max_bad: int = 300,
        seed: int = 42,
        pooled_bad: bool = False,
        max_type_bad: int = 2000,
        loss_year: dict | None = None,
    ):
        self.n_donors, self.max_km = n_donors, max_km
        self.donor_km = float(donor_km)
        # {forest_type: (n_donors, donor_km)}; types not named use the fields above.
        self.pool_by_type = {
            str(t): (int(n), float(km)) for t, (n, km) in (pool_by_type or {}).items()
        }
        self.pooled_bad, self.max_type_bad = bool(pooled_bad), int(max_type_bad)
        self._loss_year = loss_year or {}
        self._type_bad_cache: dict[tuple[str, int], np.ndarray] = {}
        # Refuse rather than silently score with leakage.
        if blocks is None and exclude_block:
            raise ValueError(
                "exclude_block=True needs `blocks` (poly_id -> block id); build "
                "it with condition.blocks.assign_blocks()."
            )
        if exclude_km <= 0 and not exclude_block:
            warnings.warn(
                "no spatial exclusion on the good pool: a donor may be the "
                "stand next door. This reproduces the pre-E01 numbers and "
                "inflates the stand-grain separation (research E01/E20).",
                stacklevel=2,
            )
        self.blocks = blocks
        self.exclude_km = float(exclude_km)
        self.exclude_block = bool(exclude_block)
        self.max_per_donor, self.max_global = max_per_donor, max_global
        self.max_bad, self.seed = max_bad, seed
        self._rng = np.random.default_rng(seed)

        self.good_mat, self._good_index = _index_embeddings(pixels)
        self.bad_mat, self._bad_index = _index_embeddings(bad_pixels)

        # Donor -> the rows it contributes, fixed at its anchor year.
        ok = set(pixel_roles.loc[pixel_roles["role"] == "good", "pixel_id"])
        mem = membership[membership["pixel_id"].isin(ok)]
        by_poly = mem.groupby("poly_id")["pixel_id"].apply(np.asarray).to_dict()
        self.donors = donors.set_index("poly_id")
        self._good_rows: dict[str, np.ndarray] = {}
        self.n_cleared_dropped, self.n_donors_cleared = 0, 0
        for poly_id, row in self.donors.iterrows():
            pid = by_poly.get(poly_id)
            if pid is None:
                continue
            anchor = int(row["anchor_year"])
            if self._loss_year:
                # Drop donor pixels Hansen records as cleared before the survey date
                # (METHODS.md, E25).
                keep = np.array(
                    [not (0 < self._loss_year.get(int(p), 0) <= anchor) for p in pid],
                    dtype=bool,
                )
                if not keep.all():
                    self.n_cleared_dropped += int((~keep).sum())
                    self.n_donors_cleared += 1
                    pid = pid[keep]
                if not len(pid):
                    continue
            rows = _rows_for(self._good_index, pid, anchor)
            if len(rows) > max_per_donor:
                rows = self._rng.choice(rows, max_per_donor, replace=False)
            if len(rows):
                self._good_rows[poly_id] = rows

        # Stand -> the transformed pixels of its own annulus.
        self._bad_pixels = (
            bad_membership.groupby("poly_id")["pixel_id"].apply(np.asarray).to_dict()
        )

        # All transformed pixels serving any stand of a type (fitted once per type).
        self._bad_by_type: dict[str, np.ndarray] = {}
        if poly_type is not None:
            bt = bad_membership.assign(
                forest_type=bad_membership["poly_id"].map(poly_type)
            )
            for ftype, grp in bt.groupby("forest_type"):
                self._bad_by_type[ftype] = grp["pixel_id"].unique()

        self._neighbours = (
            neighbours.groupby("poly_id")["poly_id2"].apply(set).to_dict()
        )
        self._centroids = centroids.set_index("poly_id")
        self._donor_order: dict[str, list[str]] = {}
        # Flat arrays for a vectorised radius test; results cached per stand.
        self._cent_ids = self._centroids.index.to_numpy()
        self._cx = self._centroids["cx"].to_numpy(dtype=float)
        self._cy = self._centroids["cy"].to_numpy(dtype=float)
        self._near_cache: dict[str, set] = {}

        # The global pool is the same cloud for every stand of a type: draw it once,
        # capped per donor, and let each stand mask out its own exclusions.
        self._global: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for ftype, grp in self.donors.groupby("forest_type"):
            ids = [d for d in grp.index if d in self._good_rows]
            if not ids:
                continue
            quota = max(1, self.max_global // len(ids))
            rows, owner = [], []
            for d in ids:
                r = self._good_rows[d]
                if len(r) > quota:
                    r = self._rng.choice(r, quota, replace=False)
                rows.append(r)
                owner.append(np.repeat(d, len(r)))
            self._global[ftype] = (np.concatenate(rows), np.concatenate(owner))

    # -- good ---------------------------------------------------------------
    def pool_rule(self, forest_type: str) -> tuple[int, float]:
        """(n_donors, donor_km) for this forest type.

        Olivinskog and kalklindeskog differ in donor density, so each type may
        have its own rule (`pool_by_type`; METHODS.md, E60).
        """
        if forest_type in self.pool_by_type:
            return self.pool_by_type[forest_type]
        return int(self.n_donors), float(self.donor_km)

    def _within_km(self, poly_id: str) -> set:
        """Stands whose centroid is within `exclude_km` of this one's."""
        if self.exclude_km <= 0 or poly_id not in self._centroids.index:
            return set()
        hit = self._near_cache.get(poly_id)
        if hit is None:
            here = self._centroids.loc[poly_id]
            d = np.hypot(self._cx - float(here["cx"]), self._cy - float(here["cy"]))
            hit = set(self._cent_ids[d <= self.exclude_km * 1000.0])
            self._near_cache[poly_id] = hit
        return hit

    def excluded_for(self, poly_id: str) -> set:
        """Every stand that must not be a reference for `poly_id`.

        Itself, overlapping or buffer-near stands, and any whose centroid is
        within `exclude_km`; `exclude_block=True` adds the probe's whole block.
        """
        out = set(self._neighbours.get(poly_id, ())) | {poly_id}
        out |= self._within_km(poly_id)
        if self.exclude_block and self.blocks is not None:
            here = self.blocks.get(poly_id)
            if here is not None:
                out |= set(self.blocks.index[self.blocks == here])
        return out

    def _eligible(self, poly_id: str, forest_type: str) -> list[str]:
        """Donor stands allowed to score `poly_id`: same type, not it, not near it."""
        blocked = self.excluded_for(poly_id)
        same = self.donors.index[self.donors["forest_type"] == forest_type]
        return [d for d in same if d not in blocked and d in self._good_rows]

    def nearest_donors(self, poly_id: str, forest_type: str) -> list[str]:
        """The eligible donors closest to this stand, within `max_km`.

        With `donor_km = 0`: the nearest `n_donors`. With `donor_km > 0`: every
        eligible donor inside that radius, with `n_donors` as a floor.
        """
        if poly_id in self._donor_order:
            return self._donor_order[poly_id]
        n_donors, donor_km = self.pool_rule(forest_type)
        elig = self._eligible(poly_id, forest_type)
        if not elig:
            self._donor_order[poly_id] = []
            return []
        here = self._centroids.loc[poly_id]
        c = self._centroids.loc[elig]
        d = np.hypot(c["cx"].to_numpy() - here["cx"], c["cy"].to_numpy() - here["cy"])
        within = d <= self.max_km * 1000.0
        order = np.argsort(d[within], kind="stable")
        if donor_km > 0:
            # radius first, count as the floor
            n_in = int((d[within][order] <= donor_km * 1000.0).sum())
            order = order[: max(n_in, n_donors)]
        else:
            order = order[:n_donors]
        picked = list(np.asarray(elig)[within][order])
        self._donor_order[poly_id] = picked
        return picked

    def good_pool(
        self, poly_id: str, forest_type: str, scope: str, with_owners: bool = False
    ):
        """(pixels x 64, n_donor_stands) for scope in {"local", "global"}.

        With `with_owners=True` returns `(pixels, owners, n_donor_stands)`, the
        donor id of each row, which the donor-level bootstrap needs. There is no
        `year` argument: donors are pinned to their anchor years, so the pool
        does not move with the probe year.
        """
        if scope not in ("local", "global"):
            raise ValueError(f"scope must be 'local' or 'global', got {scope!r}")
        empty = (np.empty((0, 64), dtype=np.float32), np.empty(0, dtype=object), 0)
        if scope == "global":
            cached = self._global.get(forest_type)
            if cached is None:
                out = empty
            else:
                rows, owner = cached
                keep = ~np.isin(owner, list(self.excluded_for(poly_id)))
                out = (
                    self.good_mat[rows[keep]],
                    owner[keep],
                    int(len(np.unique(owner[keep]))),
                )
        else:
            donors = self.nearest_donors(poly_id, forest_type)
            if not donors:
                out = empty
            else:
                rows = np.concatenate([self._good_rows[d] for d in donors])
                owner = np.concatenate(
                    [np.repeat(d, len(self._good_rows[d])) for d in donors]
                )
                out = (self.good_mat[rows], owner, len(donors))
        return out if with_owners else (out[0], out[2])

    # -- bad ----------------------------------------------------------------
    def type_bad_rows(self, forest_type: str, year: int) -> np.ndarray:
        """Every transformed pixel serving any stand of `forest_type`, that year.

        Pooling the per-stand annuli per type gives one definition of
        "transformed" per type (METHODS.md, E38). Drawn once per (type, year),
        capped, and seeded from the type and year so subsets reproduce.
        """
        key = (str(forest_type), int(year))
        if key in self._type_bad_cache:
            return self._type_bad_cache[key]
        pid = self._bad_by_type.get(forest_type)
        if pid is None or not len(pid):
            out = np.empty(0, dtype=np.int64)
        else:
            out = _rows_for(self._bad_index, np.sort(np.asarray(pid)), int(year))
            if len(out) > self.max_type_bad:
                rng = np.random.default_rng(
                    (self.seed, zlib.crc32(str(forest_type).encode()), int(year))
                )
                out = rng.choice(out, self.max_type_bad, replace=False)
        self._type_bad_cache[key] = out
        return out

    def bad_pool_rows(
        self, poly_id: str, year: int, forest_type: str | None = None
    ) -> np.ndarray:
        """The embedding-matrix rows `bad_pool` draws, so callers can cluster them."""
        if self.pooled_bad:
            if forest_type is None:
                raise ValueError("pooled_bad=True needs the probe's forest_type")
            return self.type_bad_rows(forest_type, year)
        pid = self._bad_pixels.get(poly_id)
        if pid is None:
            return np.empty(0, dtype=np.int64)
        rows = _rows_for(self._bad_index, pid, year)
        if len(rows) > self.max_bad:
            # Seeded from stand and year (crc32, not hash(), which is randomised per
            # process) so re-scoring a subset reproduces the full run.
            rng = np.random.default_rng((self.seed, zlib.crc32(poly_id.encode()), year))
            rows = rng.choice(rows, self.max_bad, replace=False)
        return rows

    def bad_pool(
        self, poly_id: str, year: int, forest_type: str | None = None
    ) -> np.ndarray:
        """The probe's transformed reference cloud, in the probe year.

        Its own annulus, or all annuli of its forest type when `pooled_bad` is
        set (the deployed default). Taken in the probe year, unlike the good pool:
        transformed land has no label to go stale.
        """
        return self.bad_mat[self.bad_pool_rows(poly_id, year, forest_type)]

    # -- the naturalness axis ------------------------------------------------
    # -- probes -------------------------------------------------------------
    def probe_rows(self, pixel_ids: np.ndarray, year: int) -> np.ndarray:
        """Matrix rows for these pixels in one year, skipping ones not observed."""
        return _rows_for(self._good_index, pixel_ids, year)

    def probes(self, rows: np.ndarray) -> np.ndarray:
        """The embeddings at those rows."""
        return self.good_mat[rows]

    def sizes(self) -> pd.DataFrame:
        """Donor stands and anchor years actually available, per forest type."""
        d = self.donors.reset_index()
        return (
            d.groupby("forest_type")
            .agg(
                n_donor_stands=("poly_id", "size"),
                n_good_pixels=("n_good_pixels", "sum"),
                first_anchor=("anchor_year", "min"),
                last_anchor=("anchor_year", "max"),
            )
            .reset_index()
        )


def _index_embeddings(
    px: pd.DataFrame,
) -> tuple[np.ndarray, dict[int, tuple[np.ndarray, np.ndarray]]]:
    """(matrix, {year -> (sorted pixel_ids, their rows)}) for pool lookups."""
    mat = np.ascontiguousarray(px[list(BANDS)].to_numpy(dtype=np.float32))
    pid = px["pixel_id"].to_numpy().astype(np.int64)
    yr = px["year"].to_numpy().astype(np.int64)
    order = np.lexsort((pid, yr))  # stable: duplicates keep row order
    pid_s, yr_s = pid[order], yr[order]
    cuts = np.flatnonzero(np.diff(yr_s)) + 1
    index = {}
    for lo, hi in zip(np.r_[0, cuts], np.r_[cuts, len(yr_s)]):
        if hi > lo:
            index[int(yr_s[lo])] = (pid_s[lo:hi], order[lo:hi].astype(np.int64))
    return mat, index


def _rows_for(index: dict, pixel_ids: np.ndarray, year: int) -> np.ndarray:
    """Rows for these pixels in one year, skipping ones with no observation.

    A duplicated (pixel_id, year) resolves to its LAST row.
    """
    ent = index.get(int(year))
    q = np.asarray(pixel_ids).astype(np.int64, copy=False)
    if ent is None or not len(q):
        return np.empty(0, dtype=np.int64)
    ids, rows = ent
    pos = np.searchsorted(ids, q, side="right") - 1
    hit = pos >= 0
    hit[hit] = ids[pos[hit]] == q[hit]
    return rows[pos[hit]]
