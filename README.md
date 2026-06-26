# Hermes-PySTD-Agent

<p align="center"><em>Install Python, then run your agent directly!</em></p>

<p align="center"><strong>English</strong> | <a href="./README_CN.md">中文</a></p>

---

&emsp;&emsp;**Hermes-PySTD-Agent** (or simply **Hermes-Lite**) is a reimplementation of [hermes-agent](https://github.com/NousResearch/hermes-agent) using **only the Python standard library**. Born from the difficulty of installing the original hermes-agent in network-restricted environments, it aims to be a simple, zero-dependency, out-of-the-box AI coding assistant.

### Why Hermes-Lite?

| | hermes-agent (original) | Hermes-Lite |
|---|---|---|
| Source size | ~16 M LoC, 2082 files | ~11.7 K LoC, 44 files |
| Runtime deps | openai, fastapi, typer, httpx, pydantic, rich, … | **none** — pure Python stdlib |
| Config | `~/.hermes/config.yaml` + `auth.json` | `~/.hermes-lite/config.json` |
| Frontend | React + Vite dashboard | Single `index.html` + `app.js` + `style.css` (dark mode) |
| Tests | pytest | 97 hand-rolled tests (< 3 s) |

### Quick Start

#### 0. Prerequisites

&emsp;&emsp;Make sure Python is installed on your system, then download or clone this repository to the target machine. Use `.sh` scripts on Linux/macOS, or `.bat` scripts on Windows.

#### 1. Configure Model Provider

```bash
./start-setup.sh
```

&emsp;&emsp;Follow the prompts to configure your model provider (API key, model selection, etc.).

<p align="center"><img src="./pics/setup.jpg" alt="Setup Wizard" /></p>

<p align="center"><em>I haven't tested Anthropic API setup personally, as most providers offer OpenAI-compatible interfaces.</em></p>

#### 2. Launch

**Option 1: CLI Mode**

```bash
./start-chat.sh
```

&emsp;&emsp;Start chatting directly in the terminal. The thinking process and tool calls are displayed by default. Type `/help` to see available slash commands.

<p align="center"><img src="./pics/cli.jpg" alt="CLI Mode" /></p>

<p align="center"><em>A bit ugly, but gets the job done.</em></p>

**Option 2: Web Mode (Recommended)**

<p align="center">⚠️ Only use this when you trust your network environment. Others should not have unrestricted access to your machine and ports.</p>

```bash
./start-web.sh --host 0.0.0.0 --open
```

&emsp;&emsp;Open your browser and start using the web interface. The web UI provides convenient access to tools, skills, memory, config, and environment variables. The chat interface offers expandable/collapsible display of system prompts, thinking, and tool calls. A side panel shows actual network requests for debugging and development.

<p align="center"><img src="./pics/web.jpg" alt="Web Mode" /></p>

<p align="center"><em>Full visibility into the agent's conversation and workflow.</em></p>

### Learn More

&emsp;&emsp;Explore the rest by using it! For questions or feedback, contact: djjsy.xjh@163.com
