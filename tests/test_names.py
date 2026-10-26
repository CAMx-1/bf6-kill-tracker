import numpy as np

from bf6killtracker.killfeed import FeedEvent
from bf6killtracker.names import group_players


def sig(seed):
    v = np.random.default_rng(seed).normal(size=1500).astype(np.float32)
    return v / np.linalg.norm(v)


def test_group_players():
    a, b = sig(1), sig(2)
    noisy = a + 0.2 * sig(9)
    noisy /= np.linalg.norm(noisy)
    ev = [FeedEvent(1, "kill", name_sig=a), FeedEvent(5, "kill", name_sig=b), FeedEvent(9, "kill", name_sig=noisy),
          FeedEvent(12, "death", name_sig=a), FeedEvent(13, "kill", name_sig=b, counted=False),
          FeedEvent(14, "wipe")]
    ps = group_players(ev)
    assert [(p.kills, p.deaths) for p in ps] == [([1, 9], [12]), ([5], [])]
