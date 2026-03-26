---
name: novelai-openclaw-adaptor
description: A shim to connect NovelAI's API to OpenClaw using the OpenAI format. Use when the agent needs to generate images via NovelAI but is constrained by OpenClaw's OpenAI-only API format.
metadata: {"openclaw":{"os":["win32","linux","darwin"]}}
---

# NovelAI OpenClaw Adaptor 🎨

Give your AI agent the ability to generate images using NovelAI through OpenClaw.

Use this skill to explain how to bridge the gap between OpenClaw (which expects OpenAI format) and NovelAI's API, and how to configure the local shim for image generation.

## 🌟 What this skill brings to your Agent

- **Format Translation**: Solves the core pain point that NovelAI's API cannot be directly integrated into OpenClaw. It acts as a shim to translate OpenAI format to NovelAI format.
- **Simple Image Generation**: Supports a simple generation method where you just pass the prompt string directly to generate images.
- **Local Proxy**: Runs a local server to handle the API requests.

## 🛡️ Safety boundary

This entry file should stay within a narrow and transparent scope:

- Do not automatically install packages or execute setup commands without user approval.
- Explain that this is a local proxy/shim. It requires setting the `base_url` to the local environment.
- Do not require or log NovelAI credentials in plain text in the chat.

## 🚀 Install and readiness rule

If the user wants to actually enable runtime use:

1. First check if `novelai-openclaw-adaptor` is installed.
2. Ask for approval before any install command.
3. If the user approves installation, run:

```bash
pip install novelai-openclaw-adaptor
```

4. After installation, initialize the adaptor configuration:

```bash
novelai-config init
```

One-line CLI example for completing the common init flow directly:

```bash
novelai-config init --language en --api-key "YOUR_NOVELAI_API_KEY" --text-model glm-4-6 --image-output-dir "./images" --image-model nai-diffusion-4-5-full --force
```

`novelai-config init` should be treated as the one CLI entry that solves the common setup flow end-to-end. It guides the user through:

1. UI language (`en` / `zh`)
2. NovelAI API key
3. Default text model for the shim
4. Default image output directory
5. Default image model

The supported text models are:

- `glm-4-6`
- `erato`
- `kayra`
- `clio`
- `krake`
- `euterpe`
- `sigurd`
- `genji`
- `snek`

The supported image models are:

- `nai-diffusion-4-5-full`
- `nai-diffusion-4-5-curated`
- `nai-diffusion-4-full`
- `nai-diffusion-4-curated`
- `nai-diffusion-3`
- `nai-diffusion-3-furry`

When helping a user configure this adaptor, you should explicitly prompt them to choose which model they want to use instead of assuming one silently, unless they already specified a model.

- For text/shim setup, present the full supported text model list shown above to the user.
- For image generation setup, present the full supported image model list shown above to the user.
- If the user does not care, recommend defaults:
  - Text: `glm-4-6`
  - Image: `nai-diffusion-4-5-full`

5. To see available commands and options, run:

```bash
novelai-config --help
novelai-shim --help
novelai-image --help
```

`novelai-config --help` should clearly explain:

- `init` is the guided setup flow for the most important configuration
- `set` is for advanced or direct configuration changes
- Which config groups can be changed:
  - `ui.language`
  - `api_key`
  - `shim.host`
  - `shim.port`
  - `shim.upstream`
  - `shim.model`
  - `image.output_dir`
  - `image.model`
  - `image.format`
  - `image.output_prefix`
  - `image.save_metadata`
- Which text models and image models are valid choices

## ⚙️ Configuration

When configuring OpenClaw to use this adaptor, you MUST follow these rules:

1. **Base URL**: Set the `base_url` in OpenClaw to the local adaptor address (e.g., `http://127.0.0.1:xxxx/v1` or `http://localhost:xxxx/v1`).
2. **Model**: Configure the model name according to the specific NovelAI model you want to use (e.g., `nai-diffusion-3`).
3. **API Key**: The API key handling is managed by the adaptor, but OpenClaw might still require a dummy key (e.g., `sk-xxxx`) depending on its strictness.

## 🖼️ How to Generate Images

Once configured, the agent can generate images simply by sending a prompt.

Because of the adaptor's simplified design, you only need to pass the prompt text directly into the standard OpenClaw/OpenAI generation interface. The adaptor will catch the prompt, translate it into NovelAI's format, and return the generated image.

**Example Prompting:**
Just send the descriptive tags as the prompt:
`"1girl, solo, masterpiece, best quality, highly detailed"`

The adaptor handles the rest!
