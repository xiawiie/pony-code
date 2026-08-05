_CACHE = {}


def regional_price(sku, region, loader):
    if sku not in _CACHE:
        _CACHE[sku] = loader(sku, region)
    return _CACHE[sku]


def clear_cache():
    _CACHE.clear()
