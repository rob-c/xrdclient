from __future__ import annotations

from benchmarks.compare import Case, Outcome, _sign_test


def test_sign_test_is_one_sided() -> None:
    assert _sign_test(5, 5) == 0.03125
    assert _sign_test(4, 5) == 0.1875


def test_outcome_requires_a_faster_median_and_reliable_paired_wins() -> None:
    case = Case("operation", "latency")
    decisive = Outcome(case, {"xrdclient": [1.0] * 5, "official": [2.0] * 5})
    assert decisive.wins("xrdclient", 0.05) == (True, 0.03125)

    noisy = Outcome(
        case,
        {"xrdclient": [1.0, 1.0, 1.0, 3.0, 3.0], "official": [2.0] * 5},
    )
    assert noisy.wins("xrdclient", 0.05) == (False, 0.5)
