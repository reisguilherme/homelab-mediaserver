def available_bytes(free: int, total: int, commitments: list[int]) -> int:
    """Return capacity available after safety margin and remaining commitments."""
    if not isinstance(free, int) or isinstance(free, bool):
        raise TypeError("free must be an integer")
    if not isinstance(total, int) or isinstance(total, bool):
        raise TypeError("total must be an integer")
    if total <= 0:
        return 0
    safety = max(20_000_000_000, (total + 19) // 20)
    committed = sum(
        max(0, value)
        for value in commitments
        if isinstance(value, int) and not isinstance(value, bool)
    )
    return max(0, free - safety - committed)


def remaining_commitment(budget: int, allocated: int) -> int:
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise TypeError("budget must be an integer")
    if not isinstance(allocated, int) or isinstance(allocated, bool):
        raise TypeError("allocated must be an integer")
    return max(0, budget - allocated)
