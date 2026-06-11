"""Tests for difference-profile axis sampling."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from wizard.axes import (
    WORLDS, TEMPERAMENTS, TECH_RELATIONSHIPS, LIFE_STAGES, FRICTION_HOOKS,
    sample_profiles, render_profile,
)

AXIS_KEYS = {"world", "temperament", "tech_relationship", "life_stage",
             "age_range", "friction_hook"}


class TestSampleProfiles:
    def test_returns_count_profiles_with_all_keys(self):
        profiles = sample_profiles(8, rng=random.Random(1))
        assert len(profiles) == 8
        for p in profiles:
            assert AXIS_KEYS == set(p.keys())

    def test_large_pools_have_no_duplicates_in_batch(self):
        profiles = sample_profiles(8, rng=random.Random(2))
        for key in ("world", "temperament", "life_stage", "friction_hook"):
            values = [p[key] for p in profiles]
            assert len(values) == len(set(values)), f"duplicate {key}: {values}"

    def test_small_pool_spreads_evenly(self):
        # TECH_RELATIONSHIPS has 4 values; 8 candidates -> each used exactly twice
        profiles = sample_profiles(8, rng=random.Random(3))
        values = [p["tech_relationship"] for p in profiles]
        for v in set(values):
            assert values.count(v) == 2

    def test_at_most_one_tech_world_per_batch(self):
        for seed in range(20):
            profiles = sample_profiles(8, rng=random.Random(seed))
            tech_count = sum(1 for p in profiles if p["world"] == "tech")
            assert tech_count <= 1

    def test_exclusions_respected(self):
        used = [{
            "world": "healthcare", "temperament": "blunt",
            "tech_relationship": "professional",
            "life_stage": "new parent", "age_range": [28, 42],
            "friction_hook": FRICTION_HOOKS[0],
        }]
        profiles = sample_profiles(5, used_axes=used, rng=random.Random(4))
        for p in profiles:
            assert p["world"] != "healthcare"
            assert p["temperament"] != "blunt"
            assert p["life_stage"] != "new parent"
            assert p["friction_hook"] != FRICTION_HOOKS[0]

    def test_age_range_matches_life_stage(self):
        profiles = sample_profiles(8, rng=random.Random(5))
        for p in profiles:
            assert p["age_range"] == list(LIFE_STAGES[p["life_stage"]])

    def test_pool_exhaustion_still_returns_count(self):
        # Exclude every life stage -> module must cycle, not crash
        used = [{"life_stage": s} for s in LIFE_STAGES]
        profiles = sample_profiles(3, used_axes=used, rng=random.Random(6))
        assert len(profiles) == 3


class TestRenderProfile:
    def test_renders_all_axes(self):
        p = sample_profiles(1, rng=random.Random(7))[0]
        text = render_profile(p, 1)
        assert p["world"] in text
        assert p["temperament"] in text
        assert p["tech_relationship"] in text
        assert p["life_stage"] in text
        assert p["friction_hook"] in text
