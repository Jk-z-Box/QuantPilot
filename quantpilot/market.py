from decimal import Decimal


class MarketError(ValueError):
    pass


def decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise MarketError("invalid_number") from exc
    if not result.is_finite():
        raise MarketError("invalid_number")
    return result
