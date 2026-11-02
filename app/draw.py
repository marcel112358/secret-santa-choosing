import secrets


def make_cycle(ids: list[int], rng=None) -> dict[int, int]:
    """Return {giver: receiver} where every person gives and receives exactly once
    and the whole group forms a single loop, so nobody draws themselves."""
    if len(ids) < 3:
        raise ValueError("need at least 3 participants")
    rng = rng or secrets.SystemRandom()
    order = list(ids)
    rng.shuffle(order)
    return {giver: order[(i + 1) % len(order)] for i, giver in enumerate(order)}
