import random

import pytest

from app.draw import DrawError, make_cycle


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


def assert_single_loop(pairs, ids):
    assert set(pairs) == set(ids)
    assert sorted(pairs.values()) == sorted(ids)
    seen, current = [], ids[0]
    while True:
        seen.append(current)
        current = pairs[current]
        if current == ids[0]:
            break
    assert sorted(seen) == sorted(ids)


def couples(*pairs):
    return {frozenset(pair) for pair in pairs}


@pytest.mark.parametrize("n", range(4, 31))
def test_couples_never_draw_each_other_in_either_direction(n):
    ids = list(range(1, n + 1))
    # Pair people up as (1,2), (3,4), ... so every couple is a disjoint pair.
    forbidden = couples(*[(i, i + 1) for i in range(1, n, 2)])

    pairs = make_cycle(ids, forbidden, rng=random.Random(n))

    assert_single_loop(pairs, ids)
    for pair in forbidden:
        a, b = tuple(pair)
        assert pairs[a] != b
        assert pairs[b] != a


def test_three_people_with_one_couple_is_impossible():
    with pytest.raises(DrawError):
        make_cycle([1, 2, 3], couples((1, 2)))


def test_person_with_too_few_partners_is_reported():
    # Person 1 may not draw 2, 3 or 4, so there's no one left to give to.
    with pytest.raises(DrawError):
        make_cycle([1, 2, 3, 4], couples((1, 2), (1, 3), (1, 4)))


def test_no_constraints_still_gives_a_loop():
    ids = list(range(1, 10))
    assert_single_loop(make_cycle(ids, rng=random.Random(1)), ids)
