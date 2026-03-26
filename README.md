<h1 align="center">🎨 NovelAI OpenClaw Adaptor</h1>

<p align="center">
  A seamless shim to connect NovelAI's API to OpenClaw using the OpenAI format.
</p>

<p align="center">
  <strong>"Solving the core pain point of integrating NovelAI with OpenClaw by providing a simple, OpenAI-compatible local proxy."</strong>
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue" />
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-brightgreen" />
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#how-it-works">How It Works</a> ·
  <a href="./docs/README.zh-CN.md">简体中文</a> ·
  <a href="./docs/README.ja.md">日本語</a>
</p>

<table>
  <tr>
    <td><img alt="Preview 1" src="./docs/1.webp" /></td>
    <td><img alt="Preview 2" src="./docs/2.webp" /></td>
    <td><img alt="Preview 3" src="./docs/3.webp" /></td>
    <td><img alt="Preview 4" src="./docs/4.webp" /></td>
  </tr>
  <tr>
    <td><img alt="Preview 5" src="./docs/5.webp" /></td>
    <td><img alt="Preview 6" src="./docs/6.webp" /></td>
    <td><img alt="Preview 7" src="./docs/7.webp" /></td>
    <td><img alt="Preview 8" src="./docs/8.webp" /></td>
  </tr>
</table>

## Why NovelAI OpenClaw Adaptor? 🌟

The core pain point for many users is that **NovelAI's API cannot be directly integrated into OpenClaw**, as OpenClaw expects the standard OpenAI API format. 

To solve this, we created a shim (adaptor) that acts as a middleman. It translates OpenAI-formatted requests from OpenClaw into NovelAI-compatible requests, and returns the generated images seamlessly.

Additionally, it supports a simplified generation method: you can just pass your prompt directly to generate images without complex configurations!

<a id="quick-start"></a>

## Quick Start 🚀

📥 **Install:**

```bash
pip install novelai-openclaw-adaptor
```

⚙️ **Initialize config:**

```bash
novelai-config init
```

**One-line init example:**

```bash
novelai-config init --language en --api-key "YOUR_NOVELAI_API_KEY" --text-model glm-4-6 --image-output-dir "./images" --image-model nai-diffusion-4-5-full --force
```

▶️ **Start the text model shim:**

```bash
novelai-shim
```

🖼️ **Generate an image:**

```bash
novelai-image --prompt "1girl, solo, masterpiece, best quality"
```

❓ **Help:**

```bash
novelai-config --help
novelai-shim --help
novelai-image --help
```

**Connect to OpenClaw:**

Tell OpenClaw directly:

```text
Help me install novelai-openclaw-adaptor and add a new model provider "novelai": pip install novelai-openclaw-adaptor && novelai-config --help
```

## Supported Models

**Text models exposed by the shim:**

- `glm-4-6`
- `erato`
- `kayra`
- `clio`
- `krake`
- `euterpe`
- `sigurd`
- `genji`
- `snek`

**Image models supported by the image CLI:**

- `nai-diffusion-4-5-full`
- `nai-diffusion-4-5-curated`
- `nai-diffusion-4-full`
- `nai-diffusion-4-curated`
- `nai-diffusion-3`
- `nai-diffusion-3-furry`

<a id="how-it-works"></a>

## How It Works ✨

1. **Local Proxy:** The adaptor runs locally and provides an OpenAI-compatible endpoint (e.g., `http://localhost:xxxx/v1`).
2. **Format Translation:** When OpenClaw sends an OpenAI-style request, the adaptor translates it into NovelAI's specific parameters.
3. **Simple Prompting:** You can simply pass the prompt string to generate images.
4. **Configuration:** In OpenClaw, set the `base_url` to your local adaptor address, for example `http://127.0.0.1:11434/v1`, and configure the model name as required.

## License 📄

This project is released under the `MIT` license.
