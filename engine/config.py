# -*- coding: utf-8 -*-
"""config.py — 读 config.yaml，缺项回落默认值。"""
import os

try:
    import yaml
except ImportError:  # 极简回落：没有 yaml 就用默认
    yaml = None

DEFAULTS = {
    "scout": {
        "backend": "local",
        "ollama_url": "http://localhost:11434",
        "vl_model": "qwen2.5vl:7b",
        "text_model": "qwen3:8b",
        "api_base": "",
        "api_key": "",
        "api_model": "",
    },
    "engine": {"mode": "auto"},
}


def load_config(path=None):
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    base = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    # 依次合并 config.yaml 与 config.local.yaml（后者 gitignore，存凭证）
    for p in (base, os.path.join(os.path.dirname(base), "config.local.yaml")):
        if yaml and os.path.exists(p):
            user = yaml.safe_load(open(p, encoding="utf-8")) or {}
            for k, v in user.items():
                if isinstance(v, dict) and k in cfg:
                    cfg[k].update(v)
    # 环境变量兜底
    cfg["scout"]["api_key"] = os.environ.get("SCOUT_API_KEY") or cfg["scout"]["api_key"]
    return cfg
