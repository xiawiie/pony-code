def load_name(path):
    try:
        return path.read_bytes().decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-8 configuration") from exc
