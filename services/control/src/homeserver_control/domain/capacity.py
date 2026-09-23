def available_bytes(free: int, total: int, commitments: list[int]) -> int:
    """Return free bytes after the remaining commitments supplied by the caller."""
    if not isinstance(free, int) or isinstance(free, bool):
        raise TypeError("free must be an integer")
    if not isinstance(total, int) or isinstance(total, bool):
        raise TypeError("total must be an integer")
    if total <= 0:
        return 0
    committed = sum(
        max(0, value)
        for value in commitments
        if isinstance(value, int) and not isinstance(value, bool)
    )
    return max(0, free - committed)


def remaining_commitment(budget: int, allocated: int) -> int:
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise TypeError("budget must be an integer")
    if not isinstance(allocated, int) or isinstance(allocated, bool):
        raise TypeError("allocated must be an integer")
    return max(0, budget - allocated)
