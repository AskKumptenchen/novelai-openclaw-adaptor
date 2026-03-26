<h1 align="center">🎨 NovelAI OpenClaw Adaptor</h1>

<p align="center">
  NovelAI API を OpenClaw の OpenAI フォーマットにシームレスに接続するアダプター。
</p>

<p align="center">
  <strong>「NovelAI が OpenClaw に直接接続できないという中心的な課題を解決し、シンプルで使いやすいローカル OpenAI 互換プロキシを提供します。」</strong>
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue" />
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-brightgreen" />
</p>

<p align="center">
  <a href="#quick-start">クイックスタート</a> ·
  <a href="#how-it-works">仕組み</a> ·
  <a href="./README.md">English</a> ·
  <a href="./README.zh-CN.md">简体中文</a>
</p>

<table>
  <tr>
    <td><img alt="Preview 1" src="./1.webp" /></td>
    <td><img alt="Preview 2" src="./2.webp" /></td>
    <td><img alt="Preview 3" src="./3.webp" /></td>
    <td><img alt="Preview 4" src="./4.webp" /></td>
  </tr>
  <tr>
    <td><img alt="Preview 5" src="./5.webp" /></td>
    <td><img alt="Preview 6" src="./6.webp" /></td>
    <td><img alt="Preview 7" src="./7.webp" /></td>
    <td><img alt="Preview 8" src="./8.webp" /></td>
  </tr>
</table>

## なぜ NovelAI OpenClaw Adaptor なのか？ 🌟

多くのユーザーにとっての中心的な課題は、OpenClaw が標準の OpenAI API フォーマットを想定しているため、**NovelAI の API を OpenClaw に直接統合できない**ことです。

この問題を解決するために、私たちはこの shim（アダプター）を作成しました。これは OpenClaw からの OpenAI フォーマットのリクエストを受け取り、NovelAI 互換のリクエストに変換し、生成された画像をシームレスに返します。

さらに、非常にシンプルな呼び出し方法もサポートしています。プロンプトを直接渡すだけで画像を生成でき、複雑な設定は必要ありません！

<a id="quick-start"></a>

## クイックスタート 🚀

📥 **インストール：**

```bash
pip install novelai-openclaw-adaptor
```

⚙️ **設定を初期化：**

```bash
novelai-config init
```

▶️ **テキストモデル shim を起動：**

```bash
novelai-shim
```

🖼️ **画像を生成：**

```bash
novelai-image --prompt "1girl, solo, masterpiece, best quality"
```

❓ **ヘルプ：**

```bash
novelai-config --help
novelai-shim --help
novelai-image --help
```

**OpenClaw に接続:**

OpenClaw に次のように伝えてください:

```text
novelai-openclaw-adaptor をインストールして、新しい model provider "novelai" を追加して: pip install novelai-openclaw-adaptor && novelai-config --help
```

## 対応モデル

**shim で公開されるテキストモデル:**

- `glm-4-6`
- `erato`
- `kayra`
- `clio`
- `krake`
- `euterpe`
- `sigurd`
- `genji`
- `snek`

**画像 CLI で利用できる画像モデル:**

- `nai-diffusion-4-5-full`
- `nai-diffusion-4-5-curated`
- `nai-diffusion-4-full`
- `nai-diffusion-4-curated`
- `nai-diffusion-3`
- `nai-diffusion-3-furry`

<a id="how-it-works"></a>

## 仕組み ✨

1. **ローカルプロキシ：** アダプターはローカルで実行され、OpenAI 互換のエンドポイント（例：`http://localhost:xxxx/v1`）を提供します。
2. **フォーマット変換：** OpenClaw が OpenAI スタイルのリクエストを送信すると、アダプターはそれを NovelAI 固有のパラメータに変換します。
3. **シンプルな画像生成：** プロンプト文字列を直接渡すだけで画像を生成できます。
4. **設定方法：** OpenClaw では、`base_url` をローカルのアダプターアドレスに設定し、状況に応じてモデル名を設定するだけです。

## License 📄

このプロジェクトは `MIT` ライセンスの下で公開されています。
