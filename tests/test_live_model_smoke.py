from scripts.live_model_smoke import classify_live_error, should_fail_all_skipped


def test_classify_live_error_auth():
    assert classify_live_error(RuntimeError("HTTP 401: bad key")) == "auth"


def test_classify_live_error_rate_limit():
    assert classify_live_error(RuntimeError("HTTP 429: rate limit")) == "rate_limit"


def test_classify_live_error_network():
    assert classify_live_error(RuntimeError("Could not reach Ollama")) == "network"


def test_classify_live_error_defaults_to_code_failure():
    assert classify_live_error(RuntimeError("unexpected decode bug")) == "code_failure"


def test_should_fail_all_skipped_when_everything_skipped():
    assert should_fail_all_skipped([{"status": "skipped"}]) is True


def test_should_fail_all_skipped_when_anything_runs():
    assert should_fail_all_skipped([{"status": "skipped"}, {"status": "ok"}]) is False
