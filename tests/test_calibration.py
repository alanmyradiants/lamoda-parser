import pytest

from lamoda_parser.calibration import compare


def test_within_target_passes():
    r = compare({"A": 90, "B": 45}, {"A": 100, "B": 50})
    assert r.total_error == 0.1 and r.passed


def test_over_target_fails_and_reports_missing():
    r = compare({"A": 50}, {"A": 100, "B": 100})
    assert r.total_error == 0.75 and not r.passed
    assert r.missing_in_estimate == ["B"]


def test_wape_catches_offsetting_errors():
    r = compare({"A": 150, "B": 50}, {"A": 100, "B": 100})
    assert r.total_error == 0.0
    assert r.wape == 0.5


def test_zero_actual_rejected():
    with pytest.raises(ValueError):
        compare({"A": 1}, {"A": 0})
