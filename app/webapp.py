"""Flask 网页：发布员从挂载目录选两个 x86-64 ELF 插件，推演两种装入顺序。

只解析 ELF 元数据，不执行任何插件。配置项：
  MOUNT_DIR  挂载目录（默认 /data/plugins，容器内由 compose 挂载样例）
  PORT       监听端口（默认 8080）
"""
from __future__ import annotations

import os

from flask import Flask, render_template, request

from . import service

DEFAULT_MOUNT_DIR = "/data/plugins"


def create_app() -> Flask:
    app = Flask(__name__)
    mount_dir = os.environ.get("MOUNT_DIR", DEFAULT_MOUNT_DIR)

    @app.get("/")
    def index():
        plugins, error = _safe_list(mount_dir)
        return render_template("index.html", mount_dir=mount_dir,
                               plugins=plugins, error=error, result=None,
                               sel_a=request.args.get("a", ""),
                               sel_b=request.args.get("b", ""))

    @app.post("/simulate")
    def simulate():
        plugins, list_error = _safe_list(mount_dir)
        rel_a = request.form.get("plugin_a", "")
        rel_b = request.form.get("plugin_b", "")
        result = error = None
        try:
            result = service.run_simulation(mount_dir, rel_a, rel_b)
        except Exception as exc:  # 输入或解析问题，统一在页面展示
            error = str(exc)
        return render_template("index.html", mount_dir=mount_dir,
                               plugins=plugins, error=error or list_error,
                               result=result, sel_a=rel_a, sel_b=rel_b)

    return app


def _safe_list(mount_dir: str):
    try:
        return service.list_plugins(mount_dir), None
    except OSError as exc:
        return [], f"无法读取挂载目录 {mount_dir}：{exc}"


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
