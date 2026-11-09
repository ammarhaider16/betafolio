from datetime import datetime, timezone
import pytest
from betafolio.models import PortfolioSnapshot, Position
from betafolio.metrics import calculate_metrics


@pytest.mark.parametrize('price,previous,current,expected', [
    ('110', '100', True, 10), ('90', '100', True, -10),
    ('100', '100', True, 0), ('0', '100', True, -100),
    (None, '100', True, None), ('100', None, True, None),
    ('110', '100', False, None), ('100', '0', True, None),
])
def test_dated_security_percentage(price, previous, current, expected):
    from betafolio.metrics import daily_change_pct
    assert daily_change_pct(price, previous, current) == expected


def test_decimal_values_duplicates_and_partial_coverage():
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), cash="10", positions=[
        Position(symbol="ABC", quantity="2", price="0.1", previous_close="0.05"),
        Position(symbol="ABC", quantity="1", price="0.1"),
        Position(symbol="XYZ", quantity="5"),
    ])
    metrics = calculate_metrics(snapshot)
    assert metrics["known_value"] == "10.3"
    assert metrics["position_values"]["ABC"] == "0.3"
    assert metrics["daily_price_change_estimate"] == "0.10"
    assert metrics["missing_prices"] == ["XYZ"]
    assert not metrics["coverage_complete"]


def test_historical_risk_reports_partial_coverage():
    from betafolio.metrics import historical_risk
    from datetime import timedelta
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), cash="100", positions=[
        Position(symbol="ABC", quantity="1", price="100"),
        Position(symbol="XYZ", quantity="1", price="100"),
    ])
    start = datetime(2026, 1, 1)
    history = {str((start + timedelta(days=index)).date()): 100 + index for index in range(30)}
    result = historical_risk(snapshot, {"ABC": history})
    assert result["observations"] == 29
    assert result["missing_symbols"] == ["XYZ"]
    assert result["covered_known_value_pct"] == pytest.approx(100 / 3)
    assert result["maximum_drawdown_pct"] == 0


def test_benchmark_estimate_uses_identical_dates_and_includes_cash():
    from betafolio.metrics import benchmark_comparison
    from datetime import timedelta
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), cash="100", positions=[Position(symbol="ABC", quantity="1", price="100")])
    start = datetime(2026, 1, 1)
    dates = [str((start + timedelta(days=index)).date()) for index in range(21)]
    history = {date: 100 * 1.01 ** index for index, date in enumerate(dates)}
    benchmark = {date: 100 * 1.002 ** index for index, date in enumerate(dates)}
    result = benchmark_comparison(snapshot, {"ABC": history}, "SPY", benchmark)
    expected = (1.005 ** 20 - 1) * 100
    assert result["current_portfolio_return_estimate_pct"] == pytest.approx(expected)
    assert result["benchmark_return_pct"] == pytest.approx((1.002 ** 20 - 1) * 100)
    assert result["relative_change_percentage_points"] == pytest.approx(expected - result["benchmark_return_pct"])
