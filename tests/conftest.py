"""状态目录也放进测试临时空间，真实子进程继承同一锁目录。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
