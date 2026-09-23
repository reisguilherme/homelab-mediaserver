from homeserver_control.domain.capacity import available_bytes, remaining_commitment


def test_bytes_already_allocated_are_not_reserved_twice() -> None:
    remaining = remaining_commitment(100_000_000_000, 40_000_000_000)
    assert available_bytes(200_000_000_000, 500_000_000_000, [remaining]) == 140_000_000_000


def test_free_space_has_no_fixed_safety_margin() -> None:
    assert available_bytes(100_000_000_000, 100_000_000_000, []) == 100_000_000_000


def test_negative_commitments_do_not_create_capacity() -> None:
    assert remaining_commitment(1, 2) == 0
    assert available_bytes(100, 100, [-10]) == 100
