"""Reference pools: what "good" and "bad" mean for these two forest types, when.

The DegreeofRecovery paper scored restoration sites for *land-cover reversion*,
so its pools came from WorldCover: good = natural/semi-natural, bad = built-up
or cropland, sampled in a 3-8 km annulus around each site.

Half of that transfers. Olivinskog and kalklindeskog are not reverting from
built-up land, and both an intact kalklindeskog and a spruce plantation on top
of one are WorldCover class 10 -- so WorldCover cannot supply the GOOD pool, and
the surveyors' `tilstand` field label does it instead. WorldCover is still the
right source for the BAD pool, where it is not blind at all: it can say where
the forest has been replaced outright. See `condition.worldcover`.

Three properties of the pools built here are worth stating up front, because
each one exists to close a specific way the naive version is circular or wrong.

**1. A reference for a forest comes from that forest type, and only from
stands, never from the score's own subject.**

Two scopes are built for every stand, and both are reported, because they
answer different questions:

    good_local    the god-labelled stands of the SAME forest type nearest the
                  probe -- every one within `donor_km`, or the nearest
                  `n_donors` where that radius is empty or unset. Region,
                  terrain, phenology and acquisition history are approximately
                  matched, so a low score is about this stand rather than about
                  Nordland.
    good_global   every god-labelled stand of the same forest type. A common
                  yardstick, so scores are comparable across the whole AOI.

Neither pool ever contains the scored stand, and neither contains a stand that
touches it (`polygon_overlap`): with layers overlapping as heavily as these do,
a neighbour is partly the same ground.

Nor -- and this is the part the first version of this pipeline got wrong -- does
the good pool contain a donor **within `exclude_km` of the probe's centroid**,
3 km by default. The six layers map the same localities repeatedly and `god`
stands cluster, so excluding only the overlapping neighbours leaves a donor a
few hundred metres away on the same hillside -- often, given 2,237 ha of polygon
on 1,694 ha of ground, partly the same stand.

3 km is not a new number: it is the inner edge of the annulus the TRANSFORMED
pool has always used (`condition.worldcover`), so both pools now obey one rule.
It replaces the leave-spatial-block-out pool exclusion this pipeline shipped
first. That was measured against it at the shipped estimator over five seeds
(`condition/research/`, experiment E20) and lost on every axis that matters:

    scope   pool rule    stand AUC   pixel AUC   olivinskog px   Youden J  donors
    global  3 km            0.760       0.735        0.706         0.422     159
    global  whole block     0.705       0.716        0.687         0.382     115
    local   3 km            0.789       0.769        0.761         0.488       8
    local   whole block     0.750       0.697        0.660         0.403       8

Seed sd is 0.002-0.011, so those gaps are real. Excluding a whole locality
removes contamination and regional matching together, and `local` exists to do
regional matching -- it also left six more stands unscoreable. What blocks are
still for is the part no radius can do: holding out localities when the
threshold is fitted, and resampling them for every interval. Both of those are
unchanged and live in `condition.score`, not here. See `condition.blocks`.

**2. `tilstand` is a statement about a date, not about the record.**

A surveyor who wrote `god` in 2017 said the stand was in good condition *in
2017*. It is not evidence about 2025, and this AOI makes that concrete: the
labelled stands were surveyed from 2015 to 2025, so some labels are eight years
older than the embeddings being scored, and a stand cut in 2020 still carries
its 2017 `god`.

So a donor contributes only its **survey-year** pixels -- `anchor_year` -- and a
donor surveyed before AlphaEarth begins (2017) cannot contribute at all. The
reference cloud is therefore "stands a surveyor called god, as they looked when
that was checked", and a probe from any year is measured against it. Every
scored row carries `label_age` so a reader can see how far the anchor is from
the year being scored.

**3. A pixel, not a polygon, is the unit -- and it belongs to a set of polygons.**

The layers overlap on 24% of their combined area, so excluding the probe by
`poly_id` does not work: the probe's own ground comes back into the pool wearing
a neighbour's id. Pools are built and excluded by `pixel_id`
(`condition.membership`), which cannot leak that way. Pixels that two layers
label contradictorily are `conflict` and enter no pool.
"""

from __future__ import annotations

import warnings
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.paths import ANNULUS_INNER_M, BANDS

# The good pool's inner radius IS the transformed pool's annulus inner edge --
# one rule for both pools, and deliberately not a second tunable. Research E20
# measured 0/1/2/3/5 km and a whole-block exclusion at the shipped estimator;
# 3 km won, and it is the radius that was already justified on the other side.
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

    Prefers the full `Kartlagtda` date where a layer carries one and falls back
    to the `year` column, which the six layers type inconsistently (text in MI,
    float-as-text in NiN5k) and which therefore has to be coerced rather than
    cast. Returns a nullable Int64: a stand with no usable date cannot be a
    donor, and saying so with NA is better than guessing a year.
    """
    yr = pd.to_numeric(polygons.get("year"), errors="coerce")
    if "Kartlagtda" in polygons.columns:
        d = pd.to_datetime(polygons["Kartlagtda"], errors="coerce", format="mixed")
        yr = d.dt.year.fillna(yr)
    return yr.round().astype("Int64")


@dataclass(frozen=True)
class Donor:
    """One good-reference stand, pinned to the year its label was assigned."""

    poly_id: str
    anchor_year: int
    n_pixels: int


def anchor_donors(
    polygons: pd.DataFrame,
    pixel_roles: pd.DataFrame,
    membership: pd.DataFrame,
    aef_years: tuple[int, ...],
    min_pixels: int = 5,
) -> pd.DataFrame:
    """The god-labelled stands eligible to be references, and in which year.

    A stand qualifies only if all four hold, and the count lost at each step is
    what the caller should report:

      * `tilstand` = god;
      * its survey year falls inside the AlphaEarth record, so the label can be
        anchored to an embedding at all;
      * it still owns at least `min_pixels` pixels whose role is unambiguously
        `good` -- a pixel another layer calls `dårlig` is a `conflict` and
        counts for nobody;
      * those pixels exist in the anchor year.

    Returns `poly_id, anchor_year, n_good_pixels`.
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
    """Builds the three reference matrices a probe needs, with the leaks closed.

    Holds the embedding tables as dense float32 arrays indexed by pixel, because
    the alternative -- filtering a 1.5 M-row long frame once per stand per pool
    -- is 700+ full scans and dominates the run. Row lookup is then an integer
    take, and building a pool is `arr[rows]`.

    Everything the pools need is decided once in `__init__`:

      * `_good_rows[(poly_id)]` -- the pixel rows a donor contributes, already
        restricted to that donor's anchor year;
      * `_bad_rows[(poly_id, year)]` -- the transformed pixels of a stand's own
        annulus in the probe year;
      * `_neighbours[poly_id]` -- stands too close to be that stand's reference.

    so `good_pool` and `bad_pool` are dictionary lookups and one concatenate.
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
        # {forest_type: (n_donors, donor_km)}. A type named here uses its own
        # rule; every other type uses the two fields above. See `pool_rule`.
        self.pool_by_type = {
            str(t): (int(n), float(km)) for t, (n, km) in (pool_by_type or {}).items()
        }
        self.pooled_bad, self.max_type_bad = bool(pooled_bad), int(max_type_bad)
        self._loss_year = loss_year or {}
        self._type_bad_cache: dict[tuple[str, int], np.ndarray] = {}
        # poly_id -> spatial block. Without it `exclude_block` cannot be honoured,
        # and silently scoring with leakage would be worse than refusing to.
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
                # A `god` label describes the day the surveyor stood there, but
                # nothing stops ground that Hansen records as cleared BEFORE
                # that day from sitting inside the polygon -- and a clearcut in
                # the good pool flatters exactly the probes that look like
                # clearings. Research E25 found 1,072 such pixels across 35
                # donors; removing them costs nothing measurable (stand AUC
                # 0.8051 -> 0.8056 pooled, inside seed noise) and it is the
                # state E26's independent Hansen-label corroboration was run in,
                # so it is on rather than merely available.
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

        # Every transformed pixel serving any stand of a forest type, for the
        # naturalness axis -- which is fitted once per type and then held fixed,
        # so unlike a probe's bad pool it is not per stand and not per year.
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
        # Flat arrays so the radius test is one vectorised pass per probe rather
        # than a DataFrame lookup per candidate donor; the result is cached
        # because `good_pool` asks for the same exclusion set once per year.
        self._cent_ids = self._centroids.index.to_numpy()
        self._cx = self._centroids["cx"].to_numpy(dtype=float)
        self._cy = self._centroids["cy"].to_numpy(dtype=float)
        self._near_cache: dict[str, set] = {}

        # The global pool is the SAME cloud for every stand of a forest type,
        # give or take the handful of donors a given stand must not see. So it
        # is drawn once, capped, and each stand masks its own exclusions out of
        # it -- rebuilding it per stand would concatenate the same 11,000 rows
        # 589 times and then throw 99% of that work away. The cap is applied
        # per donor, so one large stand cannot dominate the yardstick.
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

        The two forest types want opposite pools and the reason is donor
        density, not ecology. Over the 263 god-labelled donors, with the 3 km
        inner exclusion, a kalklindeskog stand has a MEDIAN of 54 eligible
        donors within 3-20 km; an olivinskog stand has six. So a single
        nearest-32 rule hands an olivinskog probe its 9th to 32nd reference
        from 20-50 km away, in other localities and other bedrock, while a
        single 20 km radius roughly doubles kalklindeskog's pool for no gain.
        Research E60 measures both halves of that: the radius takes olivinskog
        from 4.3 to 8.2 of its 12 field-degraded stands found and from 4.0 to
        0.9 wrongly called good, while it costs kalklindeskog 1.2 -> 3.0
        wrongly called good.

        Naming the split makes each type's rule inspectable instead of an
        emergent property of one radius, and it is why v2 ships
        `{"olivinskog": (8, 20.0)}` with kalklindeskog left on the v1 count.
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

        Itself, anything overlapping or within the neighbour buffer of it, and
        anything whose centroid is within `exclude_km` -- the same inner radius
        the transformed pool's annulus uses. `exclude_block=True` swaps that
        radius for the probe's whole spatial block, which is what this pipeline
        shipped first and what research E20 measured against it.
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

        With `donor_km = 0` this is the nearest `n_donors`, which is what the
        pipeline shipped until research E44/E59. With `donor_km > 0` it is every
        eligible donor inside that radius, and `n_donors` becomes a FLOOR: a
        probe in a donor-rich locality gets more independent support, while one
        in a sparse locality falls back to its nearest `n_donors` rather than
        being handed references from the other end of the AOI.

        The two rules are not a knob on the same quantity. A count reaches for
        its Nth-nearest donor whatever the distance -- for olivinskog, whose
        median stand has six eligible donors within 3-20 km, donors 9 to 32 of
        the deployed n=32 pool sit 20-50 km away in other localities -- while a
        radius matches each probe to its own locality's donor density.
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
            # radius first, count as the floor: whichever admits more donors
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

        With `with_owners=True` the donor id of every row is returned as well,
        as `(pixels, owners, n_donor_stands)`. The stand-level bootstrap needs
        it: resampling the pool's *rows* treats sixty pixels of one donor as
        sixty independent references and produces an interval about a third too
        narrow, so the resampling unit has to be the donor stand.

        Note there is no `year` argument. Donors are pinned to their own anchor
        years, so a stand's good pool is the same cloud whichever probe year is
        being scored -- which is the point: the yardstick must not move with the
        thing being measured.
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

        The per-stand annulus asks each probe a slightly different question: its
        transformed end-member is the cropland and built-up land that happens to
        surround it, so two stands in different landscapes are scored against
        different definitions of "transformed" and part of the ratio is
        geography rather than condition. Pooling the annuli per forest type
        makes it one definition per type.

        Measured, at the deployed local scope with three seeds, against the
        per-stand annulus (research E38): degraded recall 0.533 -> 0.581,
        degraded stands wrongly called good 0.105 -> 0.038, decided 0.558 ->
        0.616, stand AUC 0.789 -> 0.804, within-locality AUC 0.635 -> 0.689.
        Corroborated on E26's independent Hansen-cleared labels. Note that the
        stand-AUC half of that gain does not replicate on a redraw of the same
        land cover (E24: +0.001 on the v3 draw against +0.023 on the shipped
        one) while the within-locality half does, so it is the error-side
        numbers above that justify this, not the AUC.

        Drawn once per (type, year) and capped, seeded from the type and year
        rather than a shared generator, so re-scoring a subset of stands sees
        the same cloud -- the rule `bad_pool_rows` follows for the same reason.
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
            # Seeded from the stand and year rather than from a shared generator:
            # a shared one would make each stand's pool depend on how many stands
            # were drawn before it, so re-scoring a subset would not reproduce the
            # scores of the full run. crc32, not hash(): Python randomises string
            # hashing per process, so hash() would not reproduce across runs
            # either -- which is the whole point of passing a seed.
            rng = np.random.default_rng((self.seed, zlib.crc32(poly_id.encode()), year))
            rows = rng.choice(rows, self.max_bad, replace=False)
        return rows

    def bad_pool(
        self, poly_id: str, year: int, forest_type: str | None = None
    ) -> np.ndarray:
        """The probe's transformed reference cloud, in the probe year.

        Its own 3-8 km annulus, or -- when `pooled_bad` is set, which is the
        deployed default -- every annulus of its forest type pooled together.

        The bad pool IS taken contemporaneously, unlike the good pool. It can
        be: cropland and built-up are what they are in every year of the record,
        so there is no label to go stale, and a same-year reference absorbs the
        year-to-year shift of the embedding space itself.
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
    """(matrix, {year -> (sorted pixel_ids, their rows)}) -- the lookup every pool build uses."""
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
    """Rows for these pixels in one year, silently skipping ones with no observation.

    A duplicated (pixel_id, year) resolves to its LAST row, as the dict this
    replaced did.
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
