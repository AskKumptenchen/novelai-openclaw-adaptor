import argparse
import json
import sys
from typing import Any

from .config import (
    DEFAULT_CONFIG,
    SUPPORTED_LANGUAGES,
    choose_text,
    deep_merge,
    ensure_config_file,
    ensure_persisted_api_key,
    get_language,
    load_config,
    mask_secret,
    normalize_language,
    save_config,
)

TEXT_MODEL_OPTIONS = [
    ("glm-4-6", "GLM-4.6", "GLM-4.6", "Recommended default, current newest model", "默认推荐，当前最新模型"),
    ("erato", "Erato", "Erato", "Great for storytelling", "擅长故事创作"),
    ("kayra", "Kayra", "Kayra", "Stable classic model", "成熟稳定的经典模型"),
    ("clio", "Clio", "Clio", "Lighter classic model", "更轻量的经典模型"),
    ("krake", "Krake", "Krake", "Legacy large model", "旧版大模型"),
    ("euterpe", "Euterpe", "Euterpe", "Legacy classic model", "旧版经典模型"),
    ("sigurd", "Sigurd", "Sigurd", "Legacy lightweight model", "旧版轻量模型"),
    ("genji", "Genji", "Genji", "Legacy Japanese-focused model", "旧版偏日文模型"),
    ("snek", "Snek", "Snek", "Legacy code-oriented model", "旧版偏代码模型"),
]

IMAGE_MODEL_OPTIONS = [
    ("nai-diffusion-4-5-full", "NovelAI Diffusion V4.5 Full", "NovelAI Diffusion V4.5 Full", "Recommended default, latest Full model", "默认推荐，最新 Full 模型"),
    ("nai-diffusion-4-5-curated", "NovelAI Diffusion V4.5 Curated", "NovelAI Diffusion V4.5 Curated", "More conservative curated model", "更保守的 Curated 模型"),
    ("nai-diffusion-4-full", "NovelAI Diffusion V4 Full", "NovelAI Diffusion V4 Full", "Previous-generation Full model", "上一代 Full 模型"),
    ("nai-diffusion-4-curated", "NovelAI Diffusion V4 Curated", "NovelAI Diffusion V4 Curated", "Previous-generation Curated model", "上一代 Curated 模型"),
    ("nai-diffusion-3", "NovelAI Diffusion Anime V3", "NovelAI Diffusion Anime V3", "V3 anime model", "V3 动漫模型"),
    ("nai-diffusion-3-furry", "NovelAI Diffusion Furry V3", "NovelAI Diffusion Furry V3", "V3 furry model", "V3 兽人模型"),
]


def format_model_ids(options: list[tuple[str, str, str, str, str]]) -> str:
    return ", ".join(value for value, *_ in options)


TEXT_MODEL_IDS = format_model_ids(TEXT_MODEL_OPTIONS)
IMAGE_MODEL_IDS = format_model_ids(IMAGE_MODEL_OPTIONS)
TEXT_MODEL_VALUES = [value for value, *_ in TEXT_MODEL_OPTIONS]
IMAGE_MODEL_VALUES = [value for value, *_ in IMAGE_MODEL_OPTIONS]


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="Path to the TOML config file")


def resolve_config_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "config_override", None) or getattr(args, "config", None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage persistent configuration for the NovelAI OpenClaw adaptor.",
        epilog=(
            "Common workflow:\n"
            "  1. Run `novelai-config init` for guided setup.\n"
            "  2. Run `novelai-config set --help` for advanced options.\n\n"
            f"Supported text models: {TEXT_MODEL_IDS}\n"
            f"Supported image models: {IMAGE_MODEL_IDS}"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    add_config_argument(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="Run guided initialization",
        description=(
            "Guided initialization for common settings.\n\n"
            "The wizard configures:\n"
            "  1. UI language (en / zh)\n"
            "  2. NovelAI API key\n"
            "  3. Default text model for the shim\n"
            "  4. Default image output directory\n"
            "  5. Default image model\n\n"
            "You can also pass these values directly via CLI to complete init non-interactively.\n\n"
            "For advanced settings, run `novelai-config set --help`."
        ),
        epilog=(
            f"Supported text models: {TEXT_MODEL_IDS}\n"
            f"Supported image models: {IMAGE_MODEL_IDS}"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    init_parser.add_argument("--config", dest="config_override", default=None, help="Path to the TOML config file")
    init_parser.add_argument("--language", choices=SUPPORTED_LANGUAGES, default=None, help="Initialization language: en or zh")
    init_parser.add_argument("--api-key", default=None, help="Write the NovelAI API key during initialization")
    init_parser.add_argument("--text-model", choices=TEXT_MODEL_VALUES, default=None, help=f"Default text model for the shim ({TEXT_MODEL_IDS})")
    init_parser.add_argument("--image-output-dir", default=None, help="Default image output directory")
    init_parser.add_argument("--image-model", choices=IMAGE_MODEL_VALUES, default=None, help=f"Default image model ({IMAGE_MODEL_IDS})")
    init_parser.add_argument("--force", action="store_true", help="Overwrite the existing config file")

    path_parser = subparsers.add_parser("path", help="Print the config file path")
    path_parser.add_argument("--config", dest="config_override", default=None, help="Path to the TOML config file")

    show_parser = subparsers.add_parser("show", help="Print the current configuration")
    show_parser.add_argument("--config", dest="config_override", default=None, help="Path to the TOML config file")

    set_parser = subparsers.add_parser(
        "set",
        help="Update config values",
        description=(
            "Update any persisted configuration value.\n\n"
            "Main config groups:\n"
            "  - ui.language\n"
            "  - api_key\n"
            "  - shim.host / shim.port / shim.upstream / shim.model\n"
            "  - image.output_dir / image.model / image.format / image.output_prefix / image.save_metadata"
        ),
        epilog=(
            f"Supported text models for --shim-model: {TEXT_MODEL_IDS}\n"
            f"Supported image models for --image-model: {IMAGE_MODEL_IDS}"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    set_parser.add_argument("--config", dest="config_override", default=None, help="Path to the TOML config file")
    set_parser.add_argument("--api-key", default=None, help="NovelAI API key")
    set_parser.add_argument("--language", choices=SUPPORTED_LANGUAGES, default=None, help="UI language: en or zh")
    set_parser.add_argument("--shim-host", default=None, help="Shim listen host")
    set_parser.add_argument("--shim-port", type=int, default=None, help="Shim listen port")
    set_parser.add_argument("--shim-upstream", default=None, help="Shim upstream completions endpoint")
    set_parser.add_argument("--shim-model", choices=TEXT_MODEL_VALUES, default=None, help=f"Default shim model ID ({TEXT_MODEL_IDS})")
    set_parser.add_argument("--image-output-dir", default=None, help="Default image output directory")
    set_parser.add_argument("--image-model", choices=IMAGE_MODEL_VALUES, default=None, help=f"Default image model ({IMAGE_MODEL_IDS})")
    set_parser.add_argument("--image-format", choices=["png", "webp", "jpeg"], default=None, help="Default image format")
    set_parser.add_argument("--image-output-prefix", default=None, help="Default image filename prefix")
    set_parser.add_argument("--image-save-metadata", choices=["true", "false"], default=None, help="Whether to save metadata by default")
    return parser


def bool_from_str(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() == "true"


def masked_config(config: dict[str, Any]) -> dict[str, Any]:
    output = deep_merge({}, config)
    output["api_key"] = mask_secret(output.get("api_key", ""))
    return output


def ensure_interactive() -> None:
    if not sys.stdin.isatty():
        raise SystemExit(
            "The current terminal is not interactive, so guided initialization cannot run. Use `novelai-config init --api-key ...` first, then adjust other settings with `novelai-config set`."
        )


def init_requires_interaction(args: argparse.Namespace) -> bool:
    required_values = [
        args.language,
        args.api_key,
        args.text_model,
        args.image_output_dir,
        args.image_model,
    ]
    return any(value in (None, "") for value in required_values)


def prompt_with_default(prompt_text: str, default: str) -> str:
    value = input(f"{prompt_text} [{default}]: ").strip()
    return value or default


def prompt_language(default_value: str) -> str:
    print("Select language / 选择语言")
    print("  1. English -> en (default)")
    print("  2. 中文 -> zh")
    while True:
        raw = input("Enter number, press Enter for default [1]: ").strip()
        if not raw:
            return default_value
        if raw == "1":
            return "en"
        if raw == "2":
            return "zh"
        print("Invalid input. Please enter 1 or 2.")


def prompt_choice(lang: str, title: str, options: list[tuple[str, str, str, str, str]], default_value: str) -> str:
    print(title)
    default_index = 1
    for index, (value, label_en, label_zh, description_en, description_zh) in enumerate(options, start=1):
        label = choose_text(lang, label_en, label_zh)
        description = choose_text(lang, description_en, description_zh)
        marker = choose_text(lang, " (default)", " (默认)") if value == default_value else ""
        print(f"  {index}. {label} -> {value}{marker}")
        print(f"     {description}")
        if value == default_value:
            default_index = index
    while True:
        raw = input(
            choose_text(
                lang,
                f"Enter the number, or press Enter to use the default [{default_index}]: ",
                f"请输入序号，直接回车使用默认值 [{default_index}]: ",
            )
        ).strip()
        if not raw:
            return default_value
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(options):
                return options[index - 1][0]
        print(choose_text(lang, "Invalid input. Please enter one of the listed numbers.", "输入无效，请输入上面列表中的序号。"))


def run_init_wizard(config: dict[str, Any], config_path: str, args: argparse.Namespace) -> dict[str, Any]:
    config["ui"]["language"] = normalize_language(args.language or prompt_language(get_language(config)))
    lang = get_language(config)

    print(
        choose_text(
            lang,
            "Starting guided setup. Press Enter to accept the default value at each step.",
            "开始引导式初始化。每一步都可以直接回车接受默认值。",
        )
    )

    config, _, _ = ensure_persisted_api_key(
        config,
        load_config(config_path)[1],
        args.api_key,
        prompt_text=choose_text(lang, "2/5 Enter your NovelAI API key: ", "2/5 请输入 NovelAI API Key: "),
        lang=lang,
    )

    if args.text_model:
        config["shim"]["model"] = args.text_model
    else:
        config["shim"]["model"] = prompt_choice(
            lang,
            choose_text(
                lang,
                "3/5 Choose the default LLM model (based on current NovelAI docs):",
                "3/5 请选择默认 LLM 模型（根据 NovelAI 当前文档整理）:",
            ),
            TEXT_MODEL_OPTIONS,
            str(config["shim"]["model"]),
        )

    current_output_dir = str(config["image"]["output_dir"])
    if args.image_output_dir:
        config["image"]["output_dir"] = args.image_output_dir
    else:
        config["image"]["output_dir"] = prompt_with_default(
            choose_text(lang, "4/5 Enter the default image output directory", "4/5 请输入默认图片输出目录"),
            current_output_dir,
        )

    if args.image_model:
        config["image"]["model"] = args.image_model
    else:
        config["image"]["model"] = prompt_choice(
            lang,
            choose_text(
                lang,
                "5/5 Choose the default image model (based on current NovelAI docs and SDK support):",
                "5/5 请选择默认图片模型（根据 NovelAI 当前文档和 SDK 支持整理）:",
            ),
            IMAGE_MODEL_OPTIONS,
            str(config["image"]["model"]),
        )
    return config


def handle_init(args: argparse.Namespace) -> int:
    _, target_path = load_config(resolve_config_arg(args))
    init_lang = normalize_language(args.language or "en")
    if target_path.exists() and not args.force:
        raise SystemExit(
            choose_text(
                init_lang,
                f"Config already exists: {target_path}. Use --force to overwrite it.",
                f"配置已存在: {target_path}，如需覆盖请加 --force",
            )
        )
    if init_requires_interaction(args):
        ensure_interactive()
    config = deep_merge({}, DEFAULT_CONFIG)
    if args.language is not None:
        config["ui"]["language"] = normalize_language(args.language)
    lang = get_language(config)
    save_config(config, target_path)
    print(choose_text(lang, f"Created default config: {target_path}", f"已写入默认配置: {target_path}"))
    config, target_path, _ = ensure_config_file(str(target_path))
    config = run_init_wizard(config, str(target_path), args)
    save_config(config, target_path)
    lang = get_language(config)
    print(choose_text(lang, f"Initialization complete: {target_path}", f"初始化完成: {target_path}"))
    print(
        choose_text(
            lang,
            "For advanced settings, run `novelai-config set --help`.",
            "其他高级配置可使用 `novelai-config set --help` 查看并修改。",
        )
    )
    return 0


def handle_path(args: argparse.Namespace) -> int:
    _, config_path = load_config(resolve_config_arg(args))
    print(config_path)
    return 0


def handle_show(args: argparse.Namespace) -> int:
    config, config_path = load_config(resolve_config_arg(args))
    lang = get_language(config)
    print(choose_text(lang, f"config: {config_path}", f"config: {config_path}"))
    print(json.dumps(masked_config(config), ensure_ascii=False, indent=2))
    return 0


def handle_set(args: argparse.Namespace) -> int:
    config, config_path = load_config(resolve_config_arg(args))

    if args.api_key is not None:
        config["api_key"] = args.api_key
    if args.language is not None:
        config.setdefault("ui", {})["language"] = normalize_language(args.language)
    if args.shim_host is not None:
        config["shim"]["host"] = args.shim_host
    if args.shim_port is not None:
        config["shim"]["port"] = args.shim_port
    if args.shim_upstream is not None:
        config["shim"]["upstream"] = args.shim_upstream
    if args.shim_model is not None:
        config["shim"]["model"] = args.shim_model
    if args.image_output_dir is not None:
        config["image"]["output_dir"] = args.image_output_dir
    if args.image_model is not None:
        config["image"]["model"] = args.image_model
    if args.image_format is not None:
        config["image"]["format"] = args.image_format
    if args.image_output_prefix is not None:
        config["image"]["output_prefix"] = args.image_output_prefix
    metadata_flag = bool_from_str(args.image_save_metadata)
    if metadata_flag is not None:
        config["image"]["save_metadata"] = metadata_flag

    save_config(config, config_path)
    lang = get_language(config)
    print(choose_text(lang, f"Updated config: {config_path}", f"已更新配置: {config_path}"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return handle_init(args)
    if args.command == "path":
        return handle_path(args)
    if args.command == "show":
        return handle_show(args)
    if args.command == "set":
        return handle_set(args)
    raise SystemExit(f"未知命令: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
