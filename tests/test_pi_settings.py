"""pi 设置读写、校验、脱敏与文件权限。"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import pi_settings  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def main():
    d = tempfile.mkdtemp(prefix="gdd-pi-settings-")
    path = os.path.join(d, "nested", "pi-settings.json")
    old = os.environ.get("GDD_PI_SETTINGS")
    old_model = os.environ.get("GDD_AGENT_MODEL")
    os.environ["GDD_PI_SETTINGS"] = path
    os.environ.pop("GDD_AGENT_MODEL", None)
    try:
        print("[C2-1] 初次读取为空默认值")
        data = pi_settings.load()
        check(data["baseUrl"] == "" and data["model"] == "", "缺失文件返回空默认值")
        view = pi_settings.public_view(data)
        check(view["hasApiKey"] is False and view["apiKeyMasked"] == "", "未设置 key")

        print("[C2-2] 保存、权限与脱敏")
        pi_settings.save("https://api.example.com/v1", "gpt-test", api_key="sk-1234567890abcd")
        check(os.path.isfile(path), "设置文件已创建")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check(mode == 0o600, f"文件权限 0600（实际 {oct(mode)}）")
        dirmode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        check(dirmode == 0o700, f"目录权限 0700（实际 {oct(dirmode)}）")
        view = pi_settings.public_view()
        check(view["hasApiKey"] is True, "hasApiKey")
        check("sk-1234567890abcd" not in json.dumps(view), "GET 视图不含明文 key")
        check("****" in view["apiKeyMasked"], "key 已脱敏")

        print("[C2-3] 空 key 保留旧值，显式 key 覆盖")
        pi_settings.save("https://api.example.com/v1", "gpt-test-2", api_key="")
        data = pi_settings.load()
        check(data["apiKey"] == "sk-1234567890abcd", "空 key 保留旧值")
        check(data["model"] == "gpt-test-2", "模型已更新")
        pi_settings.save("http://localhost:9999/v1", "m2", api_key="new-key-123456")
        check(pi_settings.load()["apiKey"] == "new-key-123456", "新 key 覆盖")

        print("[C2-4] 非法输入被拒绝且不写文件")
        before = pi_settings.load()
        for base, model in [("", "m"), ("ftp://x", "m"), ("http://", "m"),
                            ("https://api.example.com/v1", "")]:
            try:
                pi_settings.save(base, model, api_key="x")
                check(False, f"非法输入应拒绝: {base!r}/{model!r}")
            except pi_settings.SettingsError:
                pass
        check(pi_settings.load() == before, "非法保存不落盘")

        print("[C2-5] 模型优先级与就绪判断")
        os.environ["GDD_AGENT_MODEL"] = "override-model"
        check(pi_settings.resolve_model() == "override-model", "GDD_AGENT_MODEL 优先")
        os.environ.pop("GDD_AGENT_MODEL", None)
        check(pi_settings.resolve_model() == "m2", "否则用设置页模型")
        check(pi_settings.ready_error(pi_settings.load()) == "", "配置齐全时就绪")
        check(pi_settings.ready_error({"baseUrl": "", "model": "m"}),
              "缺端点时给出设置页提示")
        check(pi_settings.ready_error({"baseUrl": "https://x/v1", "model": ""}),
              "缺模型时给出设置页提示")

        print("[C2-6] 损坏文件报可处置错误")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{bad json")
        try:
            pi_settings.load()
            check(False, "损坏文件应报错")
        except pi_settings.SettingsError as e:
            check("设置页" in str(e), "错误提示设置页")
    finally:
        if old is None:
            os.environ.pop("GDD_PI_SETTINGS", None)
        else:
            os.environ["GDD_PI_SETTINGS"] = old
        if old_model is None:
            os.environ.pop("GDD_AGENT_MODEL", None)
        else:
            os.environ["GDD_AGENT_MODEL"] = old_model
        import shutil
        shutil.rmtree(d)
    print("ALL PI SETTINGS PASSED")


if __name__ == "__main__":
    main()
