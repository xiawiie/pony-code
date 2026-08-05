_CACHE = {}


def regional_price(sku, region, loader):
    key = (sku, region)
    if key not in _CACHE:
        _CACHE[key] = loader(sku, region)
    return _CACHE[key]


def clear_cache():
    _CACHE.clear()
