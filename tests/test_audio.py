import numpy as np

from bf6killtracker.audio import STEP_S, gunfire_bursts, intensity, missed_kill_hints


def test_bursts_and_hints():
    c = np.zeros(200, np.float32)
    c[20:35] = 1.0       # 2.0-3.5 s, a kill at 3.0
    c[100:112] = 0.9     # 10-11.2 s, no kill -> hint
    c[150:151] = 1.0     # too short
    assert [(round(a, 1), round(b, 1)) for a, b in gunfire_bursts(c)] == [(2.0, 3.5), (10.0, 11.2)]
    assert missed_kill_hints(c, [3.0]) == [11.2]


def test_intensity_from_signal():
    sr = 11025
    y = np.zeros(sr * 4, np.float32)
    rng = np.random.default_rng(0)
    for t in np.arange(2.0, 3.0, 0.1):          # gunfire-like clicks in the second half
        i = int(t * sr)
        y[i:i + 200] = rng.normal(0, 0.8, 200)
    c = intensity("", y)
    assert c[int(2.2 / STEP_S):int(2.9 / STEP_S)].mean() > 3 * c[:int(1.5 / STEP_S)].mean() + 0.1
