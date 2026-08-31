from __future__ import annotations

from itertools import permutations
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.config import IdentityAlias
from unified_game_radar.errors import InputValidationError
from unified_game_radar.identity import (
    canonical_domain,
    match_identity,
    normalize_developer,
    normalize_name,
    platform_key,
)
from unified_game_radar.platform_keys import (
    MAX_SAFE_PLATFORM_ID,
    canonical_platform_key,
    parse_platform_key,
)
from unified_game_radar.schemas import GameIdentity, PlatformRecord


IDS = (
    "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b",
    "9e762f6e-dd3c-46e9-95e9-58d50ea008ad",
    "243a5dbc-47a7-45d4-ad70-b6846f2eaed0",
)


def record(
    name: str = "Echo",
    developer: str | None = "Studio A",
    platform: str = "steam",
    platform_id: str = "1",
    domain: str | None = None,
) -> PlatformRecord:
    return PlatformRecord(
        schema_version=1,
        platform=platform,
        platform_id=platform_id,
        name=name,
        developer=developer,
        official_domain=domain,
        url=f"https://example.com/{platform}/{platform_id}",
    )


def identity_for(
    source: PlatformRecord,
    opportunity_id: str = IDS[0],
    *,
    name: str | None = None,
    developer: str | None = None,
    domain: str | None = None,
    records: tuple[PlatformRecord, ...] | None = None,
) -> GameIdentity:
    display_name = source.name if name is None else name
    display_developer = source.developer if developer is None else developer
    display_domain = source.official_domain if domain is None else domain
    return GameIdentity(
        schema_version=1,
        opportunity_id=opportunity_id,
        name=display_name,
        normalized_name=normalize_name(display_name),
        developer=display_developer,
        official_domain=display_domain,
        platform_records=(source,) if records is None else records,
    )


class NameNormalizationTests(unittest.TestCase):
    def test_nfkc_casefolds_compatibility_forms(self) -> None:
        self.assertEqual(normalize_name("Ｅｃｈｏ Ⅳ"), "echo iv")
        self.assertEqual(normalize_name("Straße"), "strasse")

    def test_folds_unicode_punctuation_and_symbols_and_collapses_space(self) -> None:
        self.assertEqual(normalize_name("  Echo—Game ★  Deluxe  "), "echo game deluxe")
        self.assertEqual(normalize_developer("Studio_A/B + Co."), "studio a b co")

    def test_does_not_transliterate_unrelated_scripts(self) -> None:
        self.assertEqual(normalize_name("猫のゲーム"), "猫のゲーム")

    def test_rejects_empty_unbounded_and_control_text(self) -> None:
        for value in ("", "---", "Echo\nGame", "Echo\u200bGame", "x" * 513):
            with self.subTest(value=repr(value)):
                with self.assertRaises(InputValidationError):
                    normalize_name(value)
        with self.assertRaises(InputValidationError):
            normalize_developer("\x00Studio")


class DomainNormalizationTests(unittest.TestCase):
    def test_canonicalizes_case_idna_www_and_one_trailing_dot(self) -> None:
        self.assertEqual(canonical_domain("WWW.Example.COM."), "example.com")
        self.assertEqual(
            canonical_domain("例え.テスト"),
            "xn--r8jz45g.xn--zckzah",
        )
        self.assertIsNone(canonical_domain(None))

    def test_unicode_and_alabel_domains_are_equivalent_under_idna_2003(self) -> None:
        unicode_domain = canonical_domain("例え.テスト")
        alabel_domain = canonical_domain("xn--r8jz45g.xn--zckzah")

        self.assertEqual(unicode_domain, "xn--r8jz45g.xn--zckzah")
        self.assertEqual(alabel_domain, unicode_domain)

    def test_unicode_dot_separators_are_normalized_before_label_parsing(self) -> None:
        for separator in ("\u3002", "\uff0e", "\uff61"):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(
                    canonical_domain(f"例え{separator}テスト"),
                    "xn--r8jz45g.xn--zckzah",
                )

    def test_rejects_invalid_or_non_roundtripping_alabels(self) -> None:
        for value in ("xn--a.example", "xn--strae-oqa.de"):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    canonical_domain(value)

    def test_idna_2003_transitional_mapping_is_documented_and_deterministic(self) -> None:
        self.assertEqual(canonical_domain("faß.de"), "fass.de")
        self.assertEqual(canonical_domain("straße.de"), "strasse.de")

    def test_rejects_non_domain_inputs(self) -> None:
        invalid = (
            "https://example.com",
            "user@example.com",
            "example.com:443",
            "example.com/path",
            "example.com?q=1",
            "example.com#fragment",
            "127.0.0.1",
            "[::1]",
            "example..com",
            "-example.com",
            "example-.com",
            "example.c0m",
            "example.com..",
            "exa mple.com",
            "example\n.com",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    canonical_domain(value)


class PlatformKeyTests(unittest.TestCase):
    def test_uses_canonical_platform_specific_identifiers(self) -> None:
        self.assertEqual(platform_key(record(platform="steam", platform_id="1")), "steam:1")
        self.assertEqual(
            platform_key(record(platform="roblox", platform_id=str(2**53 - 1))),
            f"roblox:{2**53 - 1}",
        )
        self.assertEqual(
            platform_key(record(platform="itch", platform_id="studio/game_v2".replace("/", "-"))),
            "itch:studio-game_v2",
        )

    def test_rejects_noncanonical_or_unbounded_identifiers(self) -> None:
        invalid = (
            ("steam", "01"),
            ("roblox", str(2**53)),
            ("steam", "1.0"),
            ("itch", "Uppercase"),
        )
        for platform, platform_id in invalid:
            with self.subTest(platform=platform, platform_id=platform_id):
                with self.assertRaises(InputValidationError):
                    record(platform=platform, platform_id=platform_id)

    def test_shared_platform_key_helper_round_trips_boundaries(self) -> None:
        valid = (
            ("steam", str(MAX_SAFE_PLATFORM_ID)),
            ("roblox", "1"),
            ("itch", "author-game_1.2"),
        )
        for platform, platform_id in valid:
            with self.subTest(platform=platform, platform_id=platform_id):
                key = canonical_platform_key(platform, platform_id)
                self.assertEqual(parse_platform_key(key), (platform, platform_id))

        self.assertEqual(MAX_SAFE_PLATFORM_ID, 2**53 - 1)


class IdentityMatchingTests(unittest.TestCase):
    def test_exact_platform_id_is_authoritative_despite_metadata_change(self) -> None:
        stored = record("Old Name", "Old Studio", "steam", "1")
        candidate = record("Renamed", "New Studio", "steam", "1")
        self.assertEqual(match_identity(candidate, (identity_for(stored),)), IDS[0])

    def test_corrupt_duplicate_exact_key_is_ambiguous(self) -> None:
        stored = record(platform="steam", platform_id="1")
        identities = (
            identity_for(stored, IDS[0]),
            identity_for(stored, IDS[1]),
        )
        self.assertIsNone(match_identity(stored, identities))

        duplicated_row = identity_for(stored, IDS[0])
        self.assertEqual(
            match_identity(stored, (duplicated_row, duplicated_row)),
            IDS[0],
        )

    def test_same_title_and_nonempty_developer_merge_cross_platform(self) -> None:
        stored = record("Echo—Game", "Studio_A", "steam", "1")
        candidate = record("echo game", "studio a", "roblox", "2")
        self.assertEqual(match_identity(candidate, (identity_for(stored),)), IDS[0])

    def test_same_title_and_nonempty_domain_merge_cross_platform(self) -> None:
        stored = record("Echo", "Studio A", "steam", "1", "example.com")
        candidate = record("ECHO", None, "roblox", "2", "www.example.com")
        self.assertEqual(match_identity(candidate, (identity_for(stored),)), IDS[0])

    def test_explicit_aliases_are_symmetric(self) -> None:
        steam = record(platform="steam", platform_id="1")
        roblox = record("Different", "Other", "roblox", "2")
        alias = IdentityAlias(schema_version=1, source="steam:1", target="roblox:2")
        self.assertEqual(match_identity(steam, (identity_for(roblox),), (alias,)), IDS[0])
        self.assertEqual(match_identity(roblox, (identity_for(steam),), (alias,)), IDS[0])

    def test_same_title_different_developers_stay_separate(self) -> None:
        left = record("Echo", "Studio A", "steam", "1")
        right = record("Echo", "Studio B", "roblox", "2")
        self.assertIsNone(match_identity(right, (identity_for(left),), aliases={}))

    def test_missing_developer_never_merges_by_bare_title(self) -> None:
        candidates = (
            (
                record("Echo", None, "roblox", "2"),
                record("Echo", "Studio A", "steam", "1"),
            ),
            (
                record("Echo", "Studio A", "roblox", "2"),
                record("Echo", None, "steam", "1"),
            ),
            (
                record("Echo", None, "roblox", "2"),
                record("Echo", None, "steam", "1"),
            ),
        )
        for candidate, stored in candidates:
            with self.subTest(candidate=candidate, stored=stored):
                self.assertIsNone(match_identity(candidate, (identity_for(stored),)))

    def test_missing_developer_can_merge_by_domain_or_alias(self) -> None:
        steam = record("Echo", None, "steam", "1", "example.com")
        roblox = record("Echo", None, "roblox", "2", "www.example.com")
        self.assertEqual(match_identity(roblox, (identity_for(steam),)), IDS[0])
        no_domain = record("Different", None, "roblox", "3")
        alias = IdentityAlias(schema_version=1, source="steam:1", target="roblox:3")
        self.assertEqual(
            match_identity(no_domain, (identity_for(steam),), (alias,)), IDS[0]
        )

    def test_same_platform_different_ids_do_not_merge_by_metadata(self) -> None:
        stored = record("Echo", "Studio A", "steam", "1", "example.com")
        candidate = record("Echo", "Studio A", "steam", "2", "example.com")
        self.assertIsNone(match_identity(candidate, (identity_for(stored),)))

    def test_matching_is_order_independent(self) -> None:
        candidate = record("Echo", "Studio A", "roblox", "8")
        matching = identity_for(record("Echo", "Studio A", "steam", "1"), IDS[0])
        unrelated = identity_for(record("Other", "Else", "itch", "other"), IDS[1])
        outputs = {
            match_identity(candidate, identities)
            for identities in permutations((matching, unrelated))
        }
        self.assertEqual(outputs, {IDS[0]})

    def test_repeated_platform_id_reuses_stable_opportunity_id(self) -> None:
        initial = record("Echo", "Studio A", "steam", "1")
        identity = identity_for(initial, IDS[2])
        for changed in (
            initial,
            record("Echo Remastered", None, "steam", "1", "changed.example"),
        ):
            self.assertEqual(match_identity(changed, (identity,)), IDS[2])

    def test_ambiguous_metadata_matches_return_none_in_every_order(self) -> None:
        candidate = record("Echo", "Studio A", "roblox", "8")
        identities = (
            identity_for(record("Echo", "Studio A", "steam", "1"), IDS[0]),
            identity_for(record("Echo", "Studio A", "itch", "echo"), IDS[1]),
        )
        for ordering in permutations(identities):
            self.assertIsNone(match_identity(candidate, ordering))

    def test_duplicate_metadata_matches_for_same_opportunity_are_stable(self) -> None:
        candidate = record("Echo", "Studio A", "roblox", "8")
        identities = (
            identity_for(record("Echo", "Studio A", "steam", "1"), IDS[0]),
            identity_for(record("Echo", "Studio A", "itch", "echo"), IDS[0]),
        )

        self.assertEqual(match_identity(candidate, identities), IDS[0])

    def test_ambiguous_alias_matches_return_none(self) -> None:
        candidate = record("New", "New", "roblox", "8")
        identities = (
            identity_for(record("One", "One", "steam", "1"), IDS[0]),
            identity_for(record("Two", "Two", "itch", "two"), IDS[1]),
        )
        aliases = (
            IdentityAlias(1, "steam:1", "roblox:8"),
            IdentityAlias(1, "itch:two", "roblox:8"),
        )
        self.assertIsNone(match_identity(candidate, identities, aliases))

    def test_duplicate_alias_matches_for_same_opportunity_are_stable(self) -> None:
        candidate = record("New", "New", "roblox", "8")
        identities = (
            identity_for(record("One", "One", "steam", "1"), IDS[0]),
            identity_for(record("Two", "Two", "itch", "two"), IDS[0]),
        )
        aliases = (
            IdentityAlias(1, "steam:1", "roblox:8"),
            IdentityAlias(1, "itch:two", "roblox:8"),
        )

        self.assertEqual(match_identity(candidate, identities, aliases), IDS[0])

    def test_rejects_malformed_alias_values_instead_of_loose_parsing(self) -> None:
        candidate = record(platform="roblox", platform_id="2")
        stored = identity_for(record(platform="steam", platform_id="1"))
        malformed_values = (
            ({"source": "steam:1", "target": "roblox:2"},),
            (object(),),
            {"steam:1": "roblox:2"},
        )
        for aliases in malformed_values:
            with self.subTest(aliases=aliases):
                with self.assertRaises(InputValidationError):
                    match_identity(candidate, (stored,), aliases)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
