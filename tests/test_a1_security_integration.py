from copy import deepcopy
import os
from pathlib import Path
import stat
import sys


from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.providers.response import Response
from pony.runtime.options import RuntimeOptions


class ScriptedProvider:
    supports_prompt_cache = False
    last_completion_metadata = {}

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, **request):
        self.requests.append(deepcopy(request))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(stop_reason, *content):
    return Response(stop_reason=stop_reason, content=list(content), usage={})


def _build_agent(root, provider, *, session=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=provider,
        workspace=WorkspaceContext.build(
            root,
            executables={
                "python": sys.executable,
                "pytest": sys.executable,
                "sh": sys.executable,
            },
        ),
        session_store=SessionStore(root / ".pony" / "sessions"),
        session=session,
        options=RuntimeOptions(project_trusted=True, max_steps=6),
    )
    agent.set_permission_mode("default")
    return agent


def _assert_private_tree(root):
    if os.name != "posix":
        return
    assert stat.S_IMODE(root.lstat().st_mode) == 0o700, root
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            assert stat.S_IMODE(mode) == 0o700, path
        elif stat.S_ISREG(mode):
            assert stat.S_IMODE(mode) == 0o600, path


def test_security_doc_states_post_validation_and_platform_trust_boundaries():
    security_doc = (Path(__file__).resolve().parents[1] / "docs/security.md").read_text(
        encoding="utf-8"
    )

    assert "Git marker" in security_doc
    assert "结构元数据" in security_doc
    assert "config 或 index" in security_doc
    assert "校验后并发修改" in security_doc
    assert "不是 OS sandbox" in security_doc
    assert "immutable" in security_doc
    assert "snapshot" in security_doc
    assert "POSIX/macOS" in security_doc
    assert "所需安全原语不可用时 fail closed" in security_doc
    assert "Windows 等价机制留待后续设计" in security_doc
