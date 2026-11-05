import pytest
from ecomops.graph import recommend


@pytest.mark.parametrize('stocked,reserved,expected', [(5, 2, 27), (0, 0, 30), (10, 0, None), (50, 0, None)])
def test_available_stock_policy(stocked, reserved, expected):
    state = {'threshold': 10, 'target': 30, 'inventory': [{
        'id': 'test-item', 'sku': 'TEST', 'location_levels': [{
            'location_id': 'test-location', 'stocked_quantity': stocked,
            'reserved_quantity': reserved, 'available_quantity': stocked - reserved,
        }],
    }]}
    result = recommend(state)['recommendations']
    if expected is None:
        assert result == []
    else:
        assert len(result) == 1
        assert result[0]['quantity'] == expected
        assert result[0]['reserved_quantity'] == reserved
