import secrets


class DrawError(Exception):
    """The draw can't be made under the given constraints."""


def make_cycle(ids, forbidden=frozenset(), rng=None, max_steps=50_000):
    """Return {giver: receiver} forming a single loop through all ids.

    Nobody draws themselves. `forbidden` is a set of frozenset({a, b}) pairs that must
    not draw each other in either direction (couples). Raises DrawError when no loop
    satisfies the constraints.
    """
    if len(ids) < 3:
        raise ValueError("need at least 3 participants")
    rng = rng or secrets.SystemRandom()
    allowed = {
        a: sorted(b for b in ids if b != a and frozenset((a, b)) not in forbidden)
        for a in ids
    }
    # Each person needs two valid neighbours: one to give to and one to receive from.
    if any(len(options) < 2 for options in allowed.values()):
        raise DrawError("Someone has fewer than two valid partners, so the draw can't work.")

    start = ids[0]
    path = [start]
    if not _extend(path, {start}, len(ids), allowed, rng, [max_steps]):
        raise DrawError("No valid draw exists with these couples. Remove a couple or add people.")
    return {path[i]: path[(i + 1) % len(path)] for i in range(len(path))}


def _extend(path, visited, total, allowed, rng, budget):
    if len(path) == total:
        return path[0] in allowed[path[-1]]
    options = [b for b in allowed[path[-1]] if b not in visited]
    rng.shuffle(options)
    for option in options:
        budget[0] -= 1
        if budget[0] <= 0:
            raise DrawError("Couldn't find a valid draw in time. Try again.")
        path.append(option)
        visited.add(option)
        if _extend(path, visited, total, allowed, rng, budget):
            return True
        path.pop()
        visited.discard(option)
    return False
