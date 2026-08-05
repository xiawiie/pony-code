import re

_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")


def update_env(path, assignments):
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    for item in assignments:
        key, value = item.split("=", 1)
        values[key] = value
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
    for key, value in values.items():
        if not _KEY.fullmatch(key):
            raise ValueError("invalid key")
        if key not in seen:
            rendered.append(f"{key}={value}")
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
