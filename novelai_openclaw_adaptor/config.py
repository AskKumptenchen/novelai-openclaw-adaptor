import copy
import getpass
import os
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("Python 3.11+ is required") from exc


APP_NAME = "novelai-openclaw-adaptor"
SUPPORTED_LANGUAGES = ("en", "zh")


def default_output_dir() -> Path:
    return Path.home() / "Pictures" / APP_NAME


def default_config_dir() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def default_config_path() -> Path:
    return default_config_dir() / "config.toml"


def normalize_language(value: str | None) -> str:
    if not value:
        return "en"
    lowered = value.strip().lower()
    if lowered.startswith("zh"):
        return "zh"
    return "en"


def get_language(config: dict[str, Any] | None = None) -> str:
    if not config:
        return "en"
    ui = config.get("ui", {})
    if not isinstance(ui, dict):
        return "en"
    return normalize_language(str(ui.get("language", "en")))


def choose_text(lang: str, english: str, chinese: str) -> str:
    return chinese if normalize_language(lang) == "zh" else english


DEFAULT_CONFIG: dict[str, Any] = {
    "api_key": "",
    "ui": {
        "language": "en",
    },
    "shim": {
        "host": "127.0.0.1",
        "port": 18089,
        "upstream": "https://text.novelai.net/oa/v1/completions",
        "model": "glm-4-6",
        "action_mode": "single-step",
    },
    "image": {
        "output_dir": str(default_output_dir()),
        "model": "nai-diffusion-4-5-full",
        "format": "png",
        "output_prefix": "novelai",
        "save_metadata": False,
    },
}


def deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def env_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    if os.environ.get("NOVELAI_API_KEY"):
        config["api_key"] = os.environ["NOVELAI_API_KEY"]

    shim: dict[str, Any] = {}
    if os.environ.get("NOVELAI_SHIM_HOST"):
        shim["host"] = os.environ["NOVELAI_SHIM_HOST"]
    if os.environ.get("NOVELAI_UPSTREAM"):
        shim["upstream"] = os.environ["NOVELAI_UPSTREAM"]
    if os.environ.get("NOVELAI_SHIM_MODEL"):
        shim["model"] = os.environ["NOVELAI_SHIM_MODEL"]
    if os.environ.get("NOVELAI_SHIM_ACTION_MODE"):
        shim["action_mode"] = os.environ["NOVELAI_SHIM_ACTION_MODE"]
    if os.environ.get("NOVELAI_SHIM_PORT"):
        shim["port"] = int(os.environ["NOVELAI_SHIM_PORT"])
    if shim:
        config["shim"] = shim

    image: dict[str, Any] = {}
    if os.environ.get("NOVELAI_IMAGE_OUTPUT_DIR"):
        image["output_dir"] = os.environ["NOVELAI_IMAGE_OUTPUT_DIR"]
    if os.environ.get("NOVELAI_IMAGE_MODEL"):
        image["model"] = os.environ["NOVELAI_IMAGE_MODEL"]
    if os.environ.get("NOVELAI_IMAGE_FORMAT"):
        image["format"] = os.environ["NOVELAI_IMAGE_FORMAT"]
    if os.environ.get("NOVELAI_IMAGE_OUTPUT_PREFIX"):
        image["output_prefix"] = os.environ["NOVELAI_IMAGE_OUTPUT_PREFIX"]
    if image:
        config["image"] = image
    return config


def load_config(path: str | None = None) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    config = deep_merge(copy.deepcopy(DEFAULT_CONFIG), env_config())
    if config_path.exists():
        loaded = tomllib.loads(config_path.read_text(encoding="utf-8"))
        config = deep_merge(config, loaded)
    return config, config_path


def ensure_config_file(path: str | None = None) -> tuple[dict[str, Any], Path, bool]:
    config, config_path = load_config(path)
    created = False
    if not config_path.exists():
        save_config(DEFAULT_CONFIG, config_path)
        config, config_path = load_config(str(config_path))
        created = True
    return config, config_path, created


def save_config(config: dict[str, Any], path: str | Path | None = None) -> Path:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(dump_toml(config), encoding="utf-8")
    return config_path


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def prompt_api_key(prompt_text: str = "Enter NovelAI API key: ", *, lang: str = "en") -> str:
    if not sys.stdin.isatty():
        raise SystemExit(
            choose_text(
                lang,
                "NovelAI API key is missing and the current terminal is not interactive. Pass --api-key or run `novelai-config set --api-key ...` first.",
                "缺少 NovelAI API Key，且当前不是交互式终端。请传入 --api-key 或先运行 `novelai-config set --api-key ...`。",
            )
        )
    while True:
        print(
            choose_text(
                lang,
                "Paste your API key and press Enter. Hidden input is used by default, so typed characters will not be shown.",
                "请直接粘贴 API Key 后按回车。默认采用隐藏输入，输入时终端不会显示字符。",
            )
        )
        try:
            value = getpass.getpass(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            print(
                choose_text(
                    lang,
                    "Hidden input is not available in this terminal. Falling back to visible input mode.",
                    "当前终端不支持隐藏输入，改为普通输入模式。",
                )
            )
            value = input(prompt_text).strip()
        if value:
            return value
        print(
            choose_text(
                lang,
                "API key cannot be empty. Please try again.",
                "API Key 不能为空，请重新输入。",
            )
        )


def ensure_persisted_api_key(
    config: dict[str, Any],
    config_path: Path,
    provided_api_key: str | None = None,
    *,
    print_messages: bool = True,
    prompt_text: str = "Enter NovelAI API key: ",
    lang: str | None = None,
) -> tuple[dict[str, Any], str, bool]:
    resolved_lang = normalize_language(lang or get_language(config))
    api_key = (provided_api_key or "").strip() or str(config.get("api_key", "")).strip()
    updated = False
    if provided_api_key and str(config.get("api_key", "")).strip() != provided_api_key.strip():
        config["api_key"] = provided_api_key.strip()
        save_config(config, config_path)
        updated = True
        api_key = provided_api_key.strip()
        if print_messages:
            print(
                choose_text(
                    resolved_lang,
                    f"Saved API key to config file: {config_path}",
                    f"已写入 API Key 到配置文件: {config_path}",
                )
            )
    if api_key:
        return config, api_key, updated

    api_key = prompt_api_key(prompt_text, lang=resolved_lang)
    config["api_key"] = api_key
    save_config(config, config_path)
    if print_messages:
        print(
            choose_text(
                resolved_lang,
                f"Saved API key to config file: {config_path}",
                f"已写入 API Key 到配置文件: {config_path}",
            )
        )
    return config, api_key, True


def dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    scalar_items: list[tuple[str, Any]] = []
    table_items: list[tuple[str, dict[str, Any]]] = []

    for key, value in data.items():
        if isinstance(value, dict):
            table_items.append((key, value))
        else:
            scalar_items.append((key, value))

    for key, value in scalar_items:
        lines.append(f"{key} = {toml_value(value)}")

    for key, value in table_items:
        if lines:
            lines.append("")
        lines.append(f"[{key}]")
        for sub_key, sub_value in value.items():
            lines.append(f"{sub_key} = {toml_value(sub_value)}")
    lines.append("")
    return "\n".join(lines)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if value is None:
        return '""'
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
