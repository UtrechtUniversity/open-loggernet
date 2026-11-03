import unittest

from openloggernet import cli


class ParseAssignmentTests(unittest.TestCase):
    def test_booleans_are_crbasic_true_and_false(self) -> None:
        self.assertEqual(cli.parse_assignment("Public.DoRead=true"), ("Public", "DoRead", -1, "long"))
        self.assertEqual(cli.parse_assignment("Public.DoRead=FALSE"), ("Public", "DoRead", 0, "long"))

    def test_whole_numbers_are_long_and_others_ieee4(self) -> None:
        self.assertEqual(cli.parse_assignment("Public.ProfileOverride=2")[2:], (2, "long"))
        self.assertEqual(cli.parse_assignment("Public.ForcedBattV=11.8")[2:], (11.8, "ieee4"))

    def test_rejects_a_missing_value(self) -> None:
        with self.assertRaises(ValueError):
            cli.parse_assignment("Public.DoRead")


if __name__ == "__main__":
    unittest.main()
