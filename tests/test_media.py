import pytest

from app.engine.media import is_complete, missing_slots


class TestMissingSlots:
    def test_untouched_card_needs_every_slot(self):
        assert missing_slots(0, 7) == [1, 2, 3, 4, 5, 6, 7]

    def test_complete_card_needs_nothing(self):
        assert missing_slots(7, 7) == []

    def test_partial_card_resumes_at_the_first_gap(self):
        # The real catalogue held cards at 1, 2, 3, 4, 5 and 6 of 7 photos --
        # left there by failed uploads and 429s. Each must resume, not restart.
        assert missing_slots(3, 7) == [4, 5, 6, 7]
        assert missing_slots(6, 7) == [7]

    def test_extra_photos_beyond_expected_are_left_alone(self):
        assert missing_slots(9, 7) == []

    def test_negative_is_rejected(self):
        with pytest.raises(ValueError):
            missing_slots(-1, 7)


class TestCompleteness:
    @pytest.mark.parametrize(
        "have,expected,complete",
        [(0, 6, False), (5, 6, False), (6, 6, True), (7, 6, True)],
    )
    def test_is_complete(self, have, expected, complete):
        assert is_complete(have, expected) is complete

    def test_partial_cards_are_not_treated_as_done(self):
        """The trap this whole module exists to avoid.

        A marketplace filter for "cards without photos" reports a card holding
        3 of 7 photos as *having* photos, so such a card would never be picked
        up again. Counting catches it.
        """
        marketplace_says_has_photos = True
        have, expected = 3, 7
        assert marketplace_says_has_photos is True
        assert is_complete(have, expected) is False
        assert missing_slots(have, expected) == [4, 5, 6, 7]
