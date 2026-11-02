import random

import pytest

from app.draw import make_cycle


@pytest.mark.parametrize("n", range(3, 51))
def test_cycle_is_single_loop_without_self_matches(n):
    ids = list(range(1, n + 1))
    pairs = make_cycle(ids, rng=random.Random(n))

    assert set(pairs) == set(ids)  # everyone gives once
    assert sorted(pairs.values()) == ids  # everyone receives once
    assert all(giver != receiver for giver, receiver in pairs.items())

    # Following givers from any start visits everyone before returning.
    seen, current = [], ids[0]
    while True:
        seen.append(current)
        current = pairs[current]
        if current == ids[0]:
            break
    assert sorted(seen) == ids


def test_rejects_fewer_than_three():
    with pytest.raises(ValueError):
        make_cycle([1, 2])
