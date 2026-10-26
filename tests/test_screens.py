from bf6killtracker.screens import ScreenTracker


def test_down_periods_and_board_views():
    st = ScreenTracker()
    for i in range(60):                       # 0.5 s steps; down from 5 s to 12 s
        st.update_down(i * 0.5, 10 <= i <= 24)
        st.update_showing(i * 0.5, 40 <= i <= 44)
    st.update_board(21.0, (100, 3, 1, 2))
    down, boards = st.finish()
    assert [(d.start, d.end) for d in down] == [(5.0, 12.0)]
    assert st._views == [[20.0, 22.0]]
    assert boards[0].kills == 3


def test_blip_is_not_a_death():
    st = ScreenTracker()
    for i in range(10):
        st.update_down(i * 0.5, i == 3)
    assert st.finish()[0] == []
