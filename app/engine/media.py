"""Which photo slots a card is still missing.

Extracted as a pure function because this is precisely where the previous system
went wrong, twice over:

* It asked a local checkpoint "did I process this card today?" instead of asking
  the marketplace "does this card have its photos?". The checkpoint reset every
  midnight, so every card was re-uploaded every night -- tens of thousands of
  redundant uploads that grew with the catalogue until a run took 38 hours.
* The obvious server-side fix, filtering for cards the marketplace reports as
  having no photos, silently excludes cards left part-way through by a failed
  upload. In the real catalogue 27 cards sat at 1-6 of their expected photos;
  that filter would have stranded every one of them permanently.

So completeness is judged by counting, and repair resumes at the first empty slot.
"""

from __future__ import annotations


def missing_slots(have: int, expected: int) -> list[int]:
    """Photo positions still to upload, given how many the card already holds.

    Slots are filled in order, so a card with ``have`` photos occupies slots
    1..have and needs have+1..expected. Slot 1 is the generated main image;
    later slots are the static extras in order.
    """
    if have < 0:
        raise ValueError("have cannot be negative")
    if have >= expected:
        return []
    return list(range(have + 1, expected + 1))


def is_complete(have: int, expected: int) -> bool:
    return have >= expected
