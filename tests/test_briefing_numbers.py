from betafolio.delivery import render_report
import pytest


@pytest.mark.parametrize('stored_percentage', [False, True])
@pytest.mark.parametrize('price,contribution,expected', [
    ('110', '20', '+10.00%; value change +$20.00'),
    ('90', '-20', '-10.00%; value change -$20.00'),
    ('100', '0', '0.00%; value change $0.00'),
])
def test_report_numbers_and_saved_report_compatibility(stored_percentage, price, contribution, expected):
    row = {'symbol': 'ABC', 'price': price, 'previous_close': '100', 'current_session': True}
    if stored_percentage:
        row['daily_change_pct'] = float(price) - 100
    _, text = render_report({'metrics': {
        'position_details': [row], 'daily_price_contributions': {'ABC': contribution},
    }})
    assert expected in text


def test_explicit_missing_percentage_does_not_fall_back_to_saved_prices():
    _, text = render_report({'metrics': {'position_details': [{
        'symbol': 'ABC', 'price': '110', 'previous_close': '100',
        'current_session': False, 'daily_change_pct': None,
    }], 'daily_price_contributions': {'ABC': '20'}}})
    assert 'day change Unavailable' in text
    assert '+10.00%' not in text and '+$20.00' not in text
