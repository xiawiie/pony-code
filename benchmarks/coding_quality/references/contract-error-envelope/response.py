from errors import AppError


def handle(action):
    try:
        return {"ok": True, "value": action()}
    except AppError as exc:
        return {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message},
        }
