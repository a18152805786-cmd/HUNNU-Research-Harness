import unittest

from hunnu_harness.literature.fulltext import (
    _extract_doi_candidates,
    _matches_spaced_doi_target,
)


class SpacedDOIParsingTests(unittest.TestCase):
    def test_normal_doi_is_unchanged(self) -> None:
        self.assertEqual(
            _extract_doi_candidates("DOI: 10.1234/abc.2024.001"),
            ("10.1234/abc.2024.001",),
        )

    def test_spaced_doi_is_normalized_only_inside_the_token(self) -> None:
        self.assertEqual(
            _extract_doi_candidates(
                "DOI: 10 . 13546 / j.cnki.tjyjc. 2023 . 18 . 032"
            ),
            ("10.13546/j.cnki.tjyjc.2023.18.032",),
        )

    def test_spaced_doi_does_not_absorb_trailing_article_number(self) -> None:
        self.assertEqual(
            _extract_doi_candidates(
                "DOI: 10 . 13546 / j.cnki.tjyjc. 2023 . 18 . 032 169"
            ),
            ("10.13546/j.cnki.tjyjc.2023.18.032",),
        )

    def test_spaced_doi_does_not_absorb_trailing_year(self) -> None:
        self.assertEqual(
            _extract_doi_candidates(
                "DOI: 10 . 13902 / j.cnki.syyj.2019.12.004 2019"
            ),
            ("10.13902/j.cnki.syyj.2019.12.004",),
        )

    def test_spaced_doi_does_not_absorb_trailing_jel(self) -> None:
        self.assertEqual(
            _extract_doi_candidates(
                "DOI: 10 . 19592 / j.cnki.scje.412230 JEL"
            ),
            ("10.19592/j.cnki.scje.412230",),
        )

    def test_target_match_requires_a_real_right_boundary(self) -> None:
        self.assertTrue(
            _matches_spaced_doi_target(
                "DOI: 10 . 1234 / abc JEL", "10.1234/abc"
            )
        )
        self.assertFalse(
            _matches_spaced_doi_target("DOI: 10.1234/abc123", "10.1234/abc")
        )
        self.assertFalse(
            _matches_spaced_doi_target(
                "DOI: 10.1234/abc.0012019", "10.1234/abc.001"
            )
        )
        self.assertFalse(
            _matches_spaced_doi_target(
                "DOI: 10.19592/j.cnki.scje.412230jel",
                "10.19592/j.cnki.scje.412230",
            )
        )
        self.assertFalse(
            _matches_spaced_doi_target(
                "DOI: 10.1 234/abc",
                "10.1234/abc",
            )
        )


if __name__ == "__main__":
    unittest.main()
