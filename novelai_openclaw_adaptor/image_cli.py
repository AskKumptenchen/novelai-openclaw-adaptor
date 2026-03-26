import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import choose_text, ensure_config_file, ensure_persisted_api_key, get_language


def import_sdk():
    try:
        from novelai import NovelAI
        from novelai.types import GenerateImageParams, I2iParams
    except ImportError as exc:
        raise SystemExit("novelai-sdk is not installed. Run: python -m pip install novelai-sdk") from exc
    return NovelAI, GenerateImageParams, I2iParams


def parse_json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_size(raw: str) -> Any:
    value = raw.strip()
    if "x" in value.lower():
        left, right = re.split(r"[xX]", value, maxsplit=1)
        return (int(left), int(right))
    return value


def parse_extra(extra_items: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for item in extra_items:
        if "=" not in item:
            raise SystemExit(f"--extra 格式错误，需为 key=value，收到: {item}")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--extra key 不能为空，收到: {item}")
        data[key] = parse_json_value(raw_value)
    return data


def sanitize_prefix(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "novelai"


def load_request_json(path_value: str | None) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser().resolve()
    return json.loads(path.read_text(encoding="utf-8"))


def build_request_data(args: argparse.Namespace, defaults: dict[str, Any]) -> dict[str, Any]:
    data = load_request_json(args.request_json_file)

    prompt = args.prompt
    if prompt:
        data["prompt"] = prompt

    model = args.model if args.model is not None else defaults["model"]
    if model:
        data["model"] = model

    if args.size:
        data["size"] = parse_size(args.size)
    if args.steps is not None:
        data["steps"] = args.steps
    if args.scale is not None:
        data["scale"] = args.scale
    if args.seed is not None:
        data["seed"] = args.seed
    if args.n_samples is not None:
        data["n_samples"] = args.n_samples
    if args.uc is not None:
        data["uc"] = args.uc
    if args.sampler is not None:
        data["sampler"] = args.sampler
    if args.smea is not None:
        data["smea"] = args.smea
    if args.smea_dyn is not None:
        data["smea_dyn"] = args.smea_dyn
    if args.dynamic_thresholding is not None:
        data["dynamic_thresholding"] = args.dynamic_thresholding
    if args.uncond_scale is not None:
        data["uncond_scale"] = args.uncond_scale
    if args.cfg_rescale is not None:
        data["cfg_rescale"] = args.cfg_rescale
    if args.quality is not None:
        data["quality"] = args.quality
    if args.decrisp_mode is not None:
        data["decrisp_mode"] = args.decrisp_mode
    if args.variety_boost is not None:
        data["variety_boost"] = args.variety_boost
    if args.furry_mode is not None:
        data["furry_mode"] = args.furry_mode
    if args.uc_preset is not None:
        data["uc_preset"] = args.uc_preset

    if args.i2i_image:
        i2i = data.get("i2i", {})
        if not isinstance(i2i, dict):
            raise SystemExit("request-json-file 中的 i2i 必须是对象")
        i2i["image"] = str(Path(args.i2i_image).expanduser().resolve())
        if args.i2i_strength is not None:
            i2i["strength"] = args.i2i_strength
        if args.i2i_noise is not None:
            i2i["noise"] = args.i2i_noise
        data["i2i"] = i2i

    data.update(parse_extra(args.extra))
    return data


def save_images(images: list[Any], output_dir: Path, prefix: str, fmt: str) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = "jpg" if fmt == "jpeg" else fmt
    saved_paths: list[Path] = []
    for index, image in enumerate(images, start=1):
        path = output_dir / f"{prefix}_{timestamp}_{index}.{ext}"
        image.save(path, format=fmt.upper())
        saved_paths.append(path)
    return saved_paths


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    return value


def save_metadata(request_data: dict[str, Any], image_paths: list[Path]) -> Path:
    meta_path = image_paths[0].with_suffix(".json")
    meta_path.write_text(
        json.dumps(
            {
                "saved_at": datetime.now().isoformat(),
                "images": [str(path) for path in image_paths],
                "request": to_jsonable(request_data),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return meta_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate images with NovelAI and save them locally.")
    parser.add_argument("--config", default=None, help="Path to the TOML config file")
    parser.add_argument("--api-key", default=None, help="NovelAI API key; overrides the config file")
    parser.add_argument("--prompt", default=None, help="Positive prompt")
    parser.add_argument("--uc", default=None, help="Negative prompt / undesired content")
    parser.add_argument("--model", default=None, help="Model name")
    parser.add_argument("--size", default="portrait", help="Image size, for example portrait / landscape / square / 832x1216")
    parser.add_argument("--steps", type=int, default=23, help="Sampling steps")
    parser.add_argument("--scale", type=float, default=5.0, help="CFG / prompt guidance scale")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument("--n-samples", type=int, default=1, help="Number of images to generate")
    parser.add_argument("--sampler", default=None, help="Sampler, for example k_euler_ancestral")
    parser.add_argument("--uc-preset", default=None, help="Negative preset, for example preset_light")
    parser.add_argument("--smea", type=parse_json_value, default=None, help="Enable SMEA, for example true")
    parser.add_argument("--smea-dyn", type=parse_json_value, default=None, help="Enable dynamic SMEA")
    parser.add_argument("--dynamic-thresholding", type=parse_json_value, default=None, help="Enable dynamic thresholding")
    parser.add_argument("--uncond-scale", type=float, default=None, help="uncond_scale")
    parser.add_argument("--cfg-rescale", type=float, default=None, help="cfg_rescale")
    parser.add_argument("--quality", type=parse_json_value, default=None, help="Enable quality enhancement")
    parser.add_argument("--decrisp-mode", type=parse_json_value, default=None, help="Enable decrisp_mode")
    parser.add_argument("--variety-boost", type=parse_json_value, default=None, help="Enable variety_boost")
    parser.add_argument("--furry-mode", type=parse_json_value, default=None, help="Enable furry_mode")
    parser.add_argument("--i2i-image", default=None, help="Input image path for img2img")
    parser.add_argument("--i2i-strength", type=float, default=None, help="img2img strength")
    parser.add_argument("--i2i-noise", type=float, default=None, help="img2img noise")
    parser.add_argument("--request-json-file", default=None, help="Advanced JSON request file; merged with CLI arguments")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help='Add any extra top-level parameter as key=value; JSON values are supported, for example --extra sampler="k_euler_ancestral"',
    )
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--output-prefix", default=None, help="Output filename prefix")
    parser.add_argument("--format", choices=["png", "webp", "jpeg"], default=None, help="Output format")
    parser.add_argument("--save-metadata", action="store_true", help="Save sidecar JSON metadata")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config, config_path, created = ensure_config_file(args.config)
    lang = get_language(config)
    if created:
        print(choose_text(lang, f"No config file found. Created default config: {config_path}", f"未发现配置文件，已自动创建默认配置: {config_path}"))

    NovelAI, GenerateImageParams, I2iParams = import_sdk()

    defaults = config["image"]
    request_data = build_request_data(args, defaults)
    if not request_data.get("prompt"):
        raise SystemExit("必须提供 --prompt，或在 --request-json-file 中提供 prompt")

    if "i2i" in request_data and isinstance(request_data["i2i"], dict):
        request_data["i2i"] = I2iParams(**request_data["i2i"])

    config, api_key, _ = ensure_persisted_api_key(
        config,
        config_path,
        args.api_key,
        prompt_text=choose_text(lang, "NovelAI API key not found. Enter it to continue image generation: ", "未检测到 NovelAI API Key，请输入后继续生图: "),
        lang=lang,
    )

    client = NovelAI(api_key=api_key)
    params = GenerateImageParams(**request_data)
    images = client.image.generate(params)

    output_dir = Path(args.output_dir or defaults["output_dir"]).expanduser().resolve()
    prefix = sanitize_prefix(args.output_prefix or defaults["output_prefix"])
    fmt = args.format or defaults["format"]
    image_paths = save_images(images, output_dir, prefix, fmt)

    meta_path = None
    save_metadata_enabled = args.save_metadata or bool(defaults.get("save_metadata"))
    if save_metadata_enabled:
        meta_path = save_metadata(request_data, image_paths)

    print(choose_text(lang, f"Config: {config_path}", f"Config: {config_path}"))
    print(choose_text(lang, "Generation complete:", "生成完成:"))
    for path in image_paths:
        print(path)
    if meta_path:
        print(f"metadata: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
