import unittest

from matching import normalize_label, optimal_one_to_one, parse_decimal


class MatchingTests(unittest.TestCase):
    def test_normalize_label_only_known_prefix(self):
        self.assertEqual(normalize_label("Kabelgroup: WLR1760-03"), "WLR1760-03")
        self.assertEqual(normalize_label(" kabelGROUP :  BEK4020-04 "), "BEK4020-04")
        self.assertEqual(normalize_label("bek4020-04"), "bek4020-04")

    def test_parse_decimal_variants(self):
        self.assertEqual(parse_decimal("195"), 195.0)
        self.assertEqual(parse_decimal("16,5"), 16.5)
        self.assertEqual(parse_decimal("196,11"), 196.11)
        self.assertEqual(parse_decimal("196.11"), 196.11)
        self.assertEqual(parse_decimal("1.234,56"), 1234.56)

    def test_one_to_one_equal_counts(self):
        left = [(0, 4.46), (1, 14.85)]
        right = [(10, 14.87), (11, 4.45)]
        pairs = set(optimal_one_to_one(left, right))
        self.assertEqual(pairs, {(0, 11), (1, 10)})

    def test_one_to_one_unequal_counts(self):
        left = [(0, 10.0), (1, 20.0)]
        right = [(10, 9.9), (11, 10.1), (12, 20.2)]
        pairs = optimal_one_to_one(left, right)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len({r for _, r in pairs}), 2)
        self.assertIn((1, 12), pairs)

    def test_case_remains_exact(self):
        self.assertNotEqual(normalize_label("ABC-01"), normalize_label("abc-01"))


if __name__ == "__main__":
    unittest.main()
