"""
test_spreads.py — unit tests for crack-spread math (pure, offline).

    python3 -m unittest -v test_spreads
"""

import unittest

from feedstock import spreads


class TestCrackSpread(unittest.TestCase):
    def test_single_crack_conversion(self):
        # RBOB $2.00/gal, WTI $80/bbl -> 2*42 - 80 = 4.0 $/bbl
        self.assertAlmostEqual(spreads.crack_spread(2.00, 80.0), 4.0, places=6)

    def test_negative_crack_when_product_cheap(self):
        # A product trading below crude on a $/bbl basis -> negative margin.
        self.assertLess(spreads.crack_spread(1.50, 80.0), 0)

    def test_321_is_2to1_weighted_average(self):
        rbob, ulsd, wti = 2.90, 3.15, 76.5
        gas = spreads.crack_spread(rbob, wti)
        dist = spreads.crack_spread(ulsd, wti)
        expected = (2 * gas + dist) / 3
        self.assertAlmostEqual(spreads.crack_321(rbob, ulsd, wti), expected, places=9)

    def test_321_between_component_cracks(self):
        # The blended crack must sit between the gasoline and distillate cracks.
        rbob, ulsd, wti = 2.90, 3.40, 76.5
        gas = spreads.crack_spread(rbob, wti)
        dist = spreads.crack_spread(ulsd, wti)
        c321 = spreads.crack_321(rbob, ulsd, wti)
        self.assertGreaterEqual(c321, min(gas, dist))
        self.assertLessEqual(c321, max(gas, dist))

    def test_compute_spreads_keys_and_values(self):
        out = spreads.compute_spreads(2.90, 3.15, 76.5)
        self.assertEqual(set(out), {
            "gasoline_crack_usd_bbl",
            "distillate_crack_usd_bbl",
            "crack_321_usd_bbl",
        })
        # Distillate ($3.15) crack should exceed gasoline ($2.90) crack here.
        self.assertGreater(out["distillate_crack_usd_bbl"],
                           out["gasoline_crack_usd_bbl"])

    def test_bom_documents_tio2_gap(self):
        # The honesty contract: TiO2 is documented with no free proxy.
        self.assertIn("tio2_pigment", spreads.COATINGS_BOM)
        self.assertIsNone(spreads.COATINGS_BOM["tio2_pigment"]["proxy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
