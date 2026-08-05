def merge_config(base, overlay):
    result = dict(base)
    result.update(overlay)
    return result
