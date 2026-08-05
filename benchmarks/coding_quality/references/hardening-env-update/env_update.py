import re

_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")


def _validated(assignments):
    values = {}
    for item in assignments:
        key, separator, value = item.partition("=")
        if not separator or not _KEY.fullmatch(key) or key in values:
            raise ValueError("invalid or duplicate assignment")
        values[key] = value
    return values


def update_env(path, assignments):
    values = _validated(assignments)
    lines = path.read_text(encoding="utf-8").splitlines()
    rendered = []
    seen = set()
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            rendered.append(line)
            continue
        key, _ = line.split("=", 1)
        if key in values:
            rendered.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            rendered.append(line)
    rendered.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
