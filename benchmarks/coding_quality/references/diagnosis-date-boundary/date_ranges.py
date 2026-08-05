from datetime import date, timedelta


def inclusive_dates(start: date, end: date):
    if end < start:
        raise ValueError("end before start")
    current = start
    result = []
    while current <= end:
        result.append(current)
        current += timedelta(days=1)
    return result
