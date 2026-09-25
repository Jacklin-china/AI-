"""Read-only environment diagnostics; live provider checks are always explicit."""

from __future__ import annotations

from dataclasses import dataclass

from kantoku.config import get_settings
from kantoku.config.settings import ROOT
from kantoku.core.llm import stream_chat, vision_chat
from kantoku.core.runtime.store import RuntimeStore, configured_database_path
from kantoku.shells.image_cli import _provider


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str


def run_doctor(*, live: bool = False) -> list[Check]:
    """Check configuration and local resources without paid calls by default."""
    checks: list[Check] = []
    try:
        settings = get_settings()
        checks.append(Check("Config", "READY", "settings.yaml 已通过校验"))
    except Exception as error:
        return [Check("Config", "INVALID_CONFIG", type(error).__name__)]
    try:
        RuntimeStore(configured_database_path())
        checks.append(Check("Database", "READY", str(configured_database_path())))
    except Exception as error:
        checks.append(Check("Database", "BLOCKED", type(error).__name__))
    for name, path in (
        ("Storage", ROOT / "data"),
        ("Frontend Build", ROOT / "src" / "kantoku" / "shells" / "web" / "index.html"),
    ):
        checks.append(Check(name, "READY" if path.exists() else "BLOCKED", str(path)))
    provider_checks = (
        ("DeepSeek", settings.llm.api_key_env, settings.llm.model_chat, settings.llm.chat_api_key),
        (
            "Ark Vision", settings.llm.vision_api_key_env,
            settings.llm.model_vision, settings.llm.vision_api_key,
        ),
        (
            "Jimeng" if settings.image.provider == "volcengine-jimeng" else "Qwen Image",
            (settings.image.access_key_env if settings.image.provider == "volcengine-jimeng"
             else settings.image.api_key_env),
            settings.image.model,
            (settings.image.access_key if settings.image.provider == "volcengine-jimeng"
             else settings.image.api_key),
        ),
    )
    for name, env_name, model, credential in provider_checks:
        try:
            configured = bool(credential())
        except Exception:
            configured = False
        detail = f"{model} · {'凭据已配置' if configured else f'缺少 {env_name}'}"
        status = "READY" if configured else "BLOCKED"
        if name == "Qwen Image" and configured:
            try:
                _provider().validate_request(
                    prompt="配置检查", shot_no=1, reference_urls=(), seed=None,
                )
            except Exception as error:
                status = "BLOCKED"
                detail += f" · 业务空间地址未就绪 ({type(error).__name__})"
        if live and status == "READY":
            try:
                if name == "DeepSeek":
                    list(stream_chat(
                        [{"role": "user", "content": "Reply exactly: OK"}], timeout_s=10
                    ))
                    detail += " · live text OK"
                elif name == "Ark Vision":
                    vision_chat(
                        [{"role": "user", "content": "Reply exactly: OK"}], timeout_s=10
                    )
                    detail += " · live vision endpoint OK"
                else:
                    access = _provider().check_access()
                    if access.authenticated is False:
                        raise RuntimeError("provider authentication rejected")
                    if not access.service_ready:
                        status = "BLOCKED"
                        detail += " · no reliable free generation probe"
                    else:
                        detail += " · live query completed, no generation"
            except Exception as error:
                status = "BLOCKED"
                detail += f" · live {type(error).__name__}"
        checks.append(Check(name, status, detail))
    return checks


def print_doctor(*, live: bool = False) -> int:
    checks = run_doctor(live=live)
    print("Kantoku Doctor" + (" · LIVE requested" if live else " · safe local checks"))
    for item in checks:
        print(f"[{item.status:<14}] {item.name:<16} {item.detail}")
    return 0 if all(item.status == "READY" for item in checks) else 1
