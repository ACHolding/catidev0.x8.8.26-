#!/usr/bin/env python3
"""
CatIDE 0.1 — Cats-branded IDE importing Cursor 3 agent-first structure
Cats Agents Window (default), tiled agents, local↔cloud handoff, diffs/PR
Design Mode, Command Palette, Inline Edit (⌘K)

Free AI routing (no paid CatIDE key):
  1) Local LLMs auto-detected: LM Studio (:1234) and Ollama (:11434)
  2) Puter.js in the window (sign in once — user-pays free tier)
  3) Optional free keys: GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY,
     POLLINATIONS_API_KEY / CATS_AI_KEY, or OPENAI_API_KEY + OPENAI_BASE_URL

Env overrides: LM_STUDIO_HOST, LM_STUDIO_MODEL, OLLAMA_HOST, OLLAMA_MODEL,
CATS_LLM_BASE, CATS_LLM_MODEL
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_NAME = "CatIDE0.1"
WORKSPACE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
AGENT_MAX_STEPS = 5  # keep free-tier queues happier
_WINDOW = None  # pywebview window (for native folder dialog)


def set_workspace(path: str | Path) -> Path:
    """Switch the active workspace folder."""
    global WORKSPACE
    target = Path(path).expanduser().resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"Not a folder: {target}")
    WORKSPACE = target
    return WORKSPACE

# Free AI routing (no paid CatIDE key required).
# Order: local LLM (LM Studio / Ollama auto-detect) → env keys → Pollinations → Puter (UI).
# Cloudflare blocks Python's default User-Agent with 403/1010 — use a browser UA.
AI_HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://pollinations.ai/",
    "Origin": "https://pollinations.ai",
}
AI_GET_BASE = "https://text.pollinations.ai/"
# Native OpenAI "tools" often 402 on anonymous public tiers — use text ```tool protocol.
AI_NATIVE_TOOLS = False
OLLAMA_HOST = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or ""
LM_STUDIO_HOST = (os.environ.get("LM_STUDIO_HOST") or "http://127.0.0.1:1234").rstrip("/")
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL") or ""
CATS_LLM_BASE = (os.environ.get("CATS_LLM_BASE") or "").strip().rstrip("/")
CATS_LLM_MODEL = (os.environ.get("CATS_LLM_MODEL") or "").strip()
# Optional free-tier keys (any one is enough): GROQ_API_KEY, OPENROUTER_API_KEY,
# GEMINI_API_KEY / GOOGLE_API_KEY, POLLINATIONS_API_KEY / CATS_AI_KEY (+ optional CATS_AI_BASE).
_LAST_AI_PROVIDER = "none"
_LAST_AI_MODEL = ""
_LOCAL_LLM_CACHE: dict = {"ts": 0.0, "backends": []}
_LOCAL_LLM_CACHE_TTL = 20.0  # seconds — re-probe when servers start/stop

# Cats agent tools (OpenAI function-calling schema)
AGENT_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace. Always read before editing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from workspace root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with full contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace",
            "description": "Replace an exact substring in a file (preferred for small edits).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative dir path, default '.'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": "Search workspace text for a query string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_terminal",
            "description": "Run a shell command in the workspace (tests, builds, git status, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>CatIDE0.1</title>
<style>
@import url("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Inter:wght@400;500;600&display=swap");
:root {
  --bg: #0d0d0d;
  --bg-elev: #141414;
  --bg-sidebar: #111111;
  --bg-activity: #0a0a0a;
  --bg-tab: #161616;
  --bg-tab-active: #0d0d0d;
  --bg-input: #1a1a1a;
  --bg-hover: #1c1c1c;
  --bg-active: #252525;
  --bg-panel: #0f0f0f;
  --bg-status: #0a0a0a;
  --bg-agents: #0b0b0b;
  --border: #2a2a2a;
  --border-soft: #1f1f1f;
  --fg: #e8e8e8;
  --fg-dim: #a0a0a0;
  --fg-muted: #6b6b6b;
  --accent: #81a1c1;
  --accent-2: #88c0d0;
  --green: #a3be8c;
  --orange: #ebcb8b;
  --red: #bf616a;
  --purple: #b48ead;
  --selection: #2e4052;
  --composer: #151515;
  --chip: #1e1e1e;
  --font-ui: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", "JetBrains Mono", ui-monospace, monospace;
  --activity-w: 48px;
  --sidebar-w: 260px;
  --agents-rail-w: 240px;
  --ai-w: 400px;
  --statusbar-h: 24px;
  --topbar-h: 36px;
  --menubar-h: 28px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--fg); font-family: var(--font-ui); font-size: 13px; user-select: none; }
button { font-family: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, textarea { font-family: inherit; color: inherit; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #333; border-radius: 4px; }
#app { display: flex; flex-direction: column; height: 100vh; }

/* Cats top switcher */
.topbar {
  height: var(--topbar-h); background: var(--bg-elev); border-bottom: 1px solid var(--border-soft);
  display: flex; align-items: center; padding: 0 10px; gap: 10px; flex-shrink: 0;
}
.topbar .brand {
  display: flex; align-items: baseline; gap: 6px; font-weight: 600; font-size: 13px;
  letter-spacing: -0.2px; color: var(--fg);
}
.topbar .brand em { font-style: normal; font-weight: 700; color: #fff; font-size: 14px; }
.topbar .brand span { color: var(--accent-2); font-weight: 500; font-size: 12px; }

.layout-switch {
  display: flex; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 2px; gap: 2px;
}
.layout-switch button {
  padding: 4px 12px; border-radius: 6px; font-size: 12px; color: var(--fg-dim); font-weight: 500;
}
.layout-switch button.active { background: var(--bg-active); color: #fff; }
.topbar .spacer { flex: 1; }
.topbar .env-chips { display: flex; gap: 6px; }
.chip {
  font-size: 11px; padding: 3px 8px; border-radius: 999px; background: var(--chip);
  border: 1px solid var(--border); color: var(--fg-dim);
}
.chip.on { border-color: var(--accent); color: var(--accent-2); }
.topbar .icon-btn {
  width: 28px; height: 28px; border-radius: 6px; color: var(--fg-dim);
  display: flex; align-items: center; justify-content: center;
}
.topbar .icon-btn:hover { background: var(--bg-hover); color: #fff; }

.menubar {
  height: var(--menubar-h); background: var(--bg); display: flex; align-items: center;
  padding: 0 8px; gap: 2px; border-bottom: 1px solid var(--border-soft); flex-shrink: 0;
}
.menubar button { padding: 3px 8px; border-radius: 4px; font-size: 12px; color: var(--fg-dim); }
.menubar button:hover { background: var(--bg-hover); color: #fff; }

.body { flex: 1; display: flex; min-height: 0; }

/* Activity bar */
.activity {
  width: var(--activity-w); background: var(--bg-activity); border-right: 1px solid var(--border-soft);
  display: flex; flex-direction: column; align-items: center; padding: 6px 0; gap: 2px;
}
.act-btn {
  width: 40px; height: 40px; border-radius: 8px; color: #6e6e6e;
  display: flex; align-items: center; justify-content: center; position: relative;
}
.act-btn:hover { color: #ddd; background: var(--bg-hover); }
.act-btn.active { color: #fff; background: var(--bg-active); }
.act-btn.active::before {
  content: ""; position: absolute; left: -6px; top: 10px; bottom: 10px; width: 2px;
  background: var(--accent-2); border-radius: 2px;
}
.act-btn svg { width: 20px; height: 20px; fill: currentColor; }
.activity .spacer { flex: 1; }

.sidebar {
  width: var(--sidebar-w); background: var(--bg-sidebar); border-right: 1px solid var(--border-soft);
  display: flex; flex-direction: column; min-width: 180px; max-width: 420px;
}
.sidebar.hidden { display: none; }
.sidebar-header {
  height: 36px; display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px; font-size: 11px; letter-spacing: 0.6px; text-transform: uppercase;
  color: var(--fg-muted); font-weight: 600;
}
.sidebar-header .actions button {
  width: 22px; height: 22px; border-radius: 4px; color: var(--fg-dim);
}
.sidebar-header .actions button:hover { background: var(--bg-hover); color: #fff; }
.sidebar-body { flex: 1; overflow: auto; padding: 4px 0; }
.search-panel { display: none; flex-direction: column; gap: 8px; padding: 8px; }
.search-panel.visible { display: flex; }
.search-panel input, .side-input {
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px;
  padding: 7px 10px; outline: none; width: 100%;
}
.search-panel input:focus, .side-input:focus { border-color: var(--accent); }
.tree-item {
  display: flex; align-items: center; gap: 6px; height: 24px; padding: 0 10px 0 12px;
  cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-radius: 4px; margin: 0 6px;
}
.tree-item:hover { background: var(--bg-hover); }
.tree-item.selected { background: var(--bg-active); }
.tree-item .chev { width: 14px; color: var(--fg-muted); font-size: 10px; text-align: center; }
.indent-1 { padding-left: 22px; } .indent-2 { padding-left: 34px; }
.indent-3 { padding-left: 46px; } .indent-4 { padding-left: 58px; }

.settings-pane { padding: 10px 12px 20px; display: flex; flex-direction: column; gap: 12px; }
.settings-pane .sec-title {
  font-size: 11px; letter-spacing: 0.6px; text-transform: uppercase;
  color: var(--fg-muted); font-weight: 600; margin-top: 4px;
}
.settings-card {
  background: var(--bg-elev); border: 1px solid var(--border-soft); border-radius: 8px;
  padding: 10px 12px; display: flex; flex-direction: column; gap: 8px;
}
.settings-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.settings-row .label { font-size: 13px; color: var(--fg); font-weight: 500; }
.settings-row .sub { font-size: 11px; color: var(--fg-muted); margin-top: 2px; line-height: 1.35; }
.settings-badge {
  font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--fg-dim); white-space: nowrap;
}
.settings-badge.on { color: var(--green); border-color: rgba(163,190,140,0.45); background: rgba(163,190,140,0.08); }
.settings-badge.off { color: var(--orange); border-color: rgba(235,203,139,0.4); background: rgba(235,203,139,0.08); }
.settings-mono {
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px;
  color: var(--fg-dim); word-break: break-all;
}
.settings-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.settings-actions button {
  background: var(--bg-input); border: 1px solid var(--border); color: var(--fg);
  border-radius: 6px; padding: 6px 10px; font-size: 12px;
}
.settings-actions button:hover { border-color: var(--accent); color: #fff; }
.settings-actions button.primary { border-color: var(--accent-2); color: var(--accent-2); }
.settings-list { margin: 0; padding-left: 16px; color: var(--fg-dim); font-size: 12px; }
.settings-list li { margin: 2px 0; }
.settings-empty { color: var(--fg-muted); font-size: 12px; }

.center { flex: 1; display: flex; flex-direction: column; min-width: 0; background: var(--bg); }
.tabs {
  height: 36px; background: var(--bg-tab); display: flex; align-items: stretch; overflow-x: auto;
  border-bottom: 1px solid var(--border-soft);
}
.tab {
  display: flex; align-items: center; gap: 6px; padding: 0 14px; min-width: 110px; max-width: 200px;
  background: transparent; border-right: 1px solid var(--border-soft); color: var(--fg-dim); font-size: 12px;
  position: relative;
}
.tab.active { background: var(--bg-tab-active); color: #fff; }
.tab.active::after {
  content: ""; position: absolute; top: 0; left: 0; right: 0; height: 1px; background: var(--accent-2);
}
.tab .close { width: 16px; height: 16px; border-radius: 4px; opacity: 0; font-size: 12px; }
.tab:hover .close, .tab.active .close { opacity: 1; }
.tab .close:hover { background: #333; }
.breadcrumb {
  height: 24px; display: flex; align-items: center; padding: 0 12px; gap: 4px;
  font-size: 12px; color: var(--fg-muted); border-bottom: 1px solid var(--border-soft);
}
.editor-wrap { flex: 1; display: flex; min-height: 0; position: relative; }
.gutter {
  width: 52px; color: #555; font-family: var(--font-mono); font-size: 12px; line-height: 20px;
  text-align: right; padding: 10px 8px 10px 0; overflow: hidden; user-select: none;
}
#code {
  width: 100%; height: 100%; border: none; outline: none; resize: none; background: var(--bg);
  color: #d6d6d6; font-family: var(--font-mono); font-size: 13px; line-height: 20px;
  padding: 10px 14px; tab-size: 4; white-space: pre; overflow: auto; caret-color: #fff;
}
#code::selection { background: var(--selection); }
.welcome {
  position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
  justify-content: center; background: radial-gradient(ellipse at 50% 20%, #15202b 0%, var(--bg) 55%);
  z-index: 2;
}
.welcome.hidden { display: none; }
.welcome h1 { font-size: 56px; font-weight: 600; color: #fff; margin-bottom: 8px; letter-spacing: -1.2px; }
.welcome h1 em { font-style: normal; color: #fff; font-weight: 700; }
.welcome .sub { color: var(--fg-muted); margin-bottom: 28px; font-size: 13px; }
.welcome .actions { display: grid; gap: 8px; min-width: 300px; }
.welcome .actions button { text-align: left; padding: 8px 0; color: var(--accent-2); font-size: 13px; }
.welcome .actions button:hover { text-decoration: underline; }
.welcome .keys { margin-top: 28px; display: flex; gap: 16px; color: var(--fg-muted); font-size: 11px; }
.welcome .keys kbd {
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px;
  font-family: var(--font-mono); margin-right: 4px;
}

.panel {
  height: 170px; background: var(--bg-panel); border-top: 1px solid var(--border);
  display: flex; flex-direction: column; min-height: 100px;
}
.panel.collapsed { display: none; }
.panel-tabs {
  height: 34px; display: flex; align-items: center; padding: 0 8px; gap: 2px;
  border-bottom: 1px solid var(--border-soft);
}
.panel-tabs .ptab {
  padding: 4px 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px;
  color: var(--fg-muted); border-radius: 4px;
}
.panel-tabs .ptab.active { color: #fff; background: var(--bg-active); }
.panel-body { flex: 1; overflow: auto; padding: 8px 12px; font-family: var(--font-mono); font-size: 12px; }
#terminal { white-space: pre-wrap; color: #ccc; }
.term-input-row {
  display: flex; gap: 8px; align-items: center; padding: 4px 12px 8px; border-top: 1px solid var(--border-soft);
}
.term-input-row .prompt { color: var(--green); font-family: var(--font-mono); font-size: 12px; }
#term-input {
  flex: 1; background: transparent; border: none; outline: none; font-family: var(--font-mono); font-size: 12px; color: #fff;
}

/* Cats Agents rail */
.agents-rail {
  width: var(--agents-rail-w); background: var(--bg-agents); border-right: 1px solid var(--border-soft);
  display: none; flex-direction: column;
}
.agents-rail.visible { display: flex; }
.agents-rail-h {
  height: 36px; display: flex; align-items: center; justify-content: space-between; padding: 0 12px;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--fg-muted); font-weight: 600;
}
.agent-list { flex: 1; overflow: auto; padding: 6px; display: flex; flex-direction: column; gap: 4px; }
.agent-card {
  text-align: left; padding: 10px; border-radius: 8px; border: 1px solid transparent; background: transparent;
  cursor: pointer;
}
.agent-card:hover { background: var(--bg-hover); }
.agent-card.active { background: var(--bg-active); border-color: var(--border); }
.agent-card:focus { outline: 1px solid var(--accent); }
.agent-card .t { font-size: 12px; font-weight: 500; color: #fff; margin-bottom: 3px; }
.agent-card .m { font-size: 11px; color: var(--fg-muted); display: flex; gap: 6px; align-items: center; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); }
.dot.cloud { background: var(--accent-2); }
.dot.idle { background: #555; }

/* AI / Agent panel */
.ai-panel {
  width: var(--ai-w); background: var(--bg-agents); border-left: 1px solid var(--border-soft);
  display: flex; flex-direction: column; min-width: 300px; max-width: 640px;
}
.ai-panel.hidden { display: none; }
.ai-panel.wide { width: 100%; max-width: none; border-left: none; }
.ai-header {
  height: 40px; display: flex; align-items: center; gap: 8px; padding: 0 12px;
  border-bottom: 1px solid var(--border-soft);
}
.ai-tabs { display: flex; gap: 4px; flex: 1; overflow-x: auto; }
.ai-tab {
  padding: 4px 10px; border-radius: 6px; font-size: 12px; color: var(--fg-dim); white-space: nowrap;
}
.ai-tab.active { background: var(--bg-active); color: #fff; }
.ai-modes { display: flex; gap: 2px; padding: 8px 12px; border-bottom: 1px solid var(--border-soft); flex-wrap: wrap; }
.ai-modes button {
  padding: 4px 10px; border-radius: 6px; font-size: 12px; color: var(--fg-dim);
}
.ai-modes button.active { background: var(--bg-active); color: #fff; }
.ai-messages { flex: 1; overflow: auto; padding: 16px; display: flex; flex-direction: column; gap: 14px; position: relative; }
.ai-empty { margin: auto; text-align: center; color: var(--fg-muted); max-width: 320px; }
.ai-empty h2 { font-size: 18px; font-weight: 560; color: #fff; margin-bottom: 8px; }
.ai-empty p { font-size: 12px; line-height: 1.55; }
.ai-empty .mode-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 18px; text-align: left; }
.ai-empty .mode-grid div {
  border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: var(--bg-elev); font-size: 11px; color: var(--fg-dim);
}
.ai-empty .mode-grid strong { display: block; color: #fff; margin-bottom: 4px; font-size: 12px; }
.msg { display: flex; flex-direction: column; gap: 6px; }
.msg .role { font-size: 11px; font-weight: 600; color: var(--fg-muted); letter-spacing: 0.3px; }
.msg.user .role { color: var(--accent-2); }
.msg.assistant .role { color: var(--purple); }
.msg .bubble {
  background: var(--bg-elev); border: 1px solid var(--border-soft); border-radius: 10px; padding: 10px 12px;
  font-size: 13px; line-height: 1.55; white-space: pre-wrap; user-select: text; word-break: break-word;
}
.msg.user .bubble { background: #121820; border-color: #1e2a38; }
.msg .bubble pre {
  background: #080808; border: 1px solid #222; border-radius: 6px; padding: 10px; overflow-x: auto;
  margin: 8px 0; font-family: var(--font-mono); font-size: 12px;
}
.msg .bubble code { font-family: var(--font-mono); font-size: 12px; background: #222; padding: 1px 4px; border-radius: 3px; }
.tool-steps { display: flex; flex-direction: column; gap: 6px; margin-bottom: 6px; }
.tool-step { border: 1px solid var(--border); border-radius: 8px; background: #101010; padding: 8px 10px; font-size: 12px; }
.tool-step .ts-name { color: var(--green); font-family: var(--font-mono); font-size: 11px; font-weight: 600; }
.tool-step .ts-args, .tool-step .ts-result {
  margin-top: 4px; color: var(--fg-muted); font-family: var(--font-mono); font-size: 11px; white-space: pre-wrap;
}
.tool-step .ts-result { max-height: 90px; overflow: auto; border-top: 1px solid #222; padding-top: 6px; }
.msg.thinking .bubble { color: var(--fg-muted); font-style: italic; border-style: dashed; }
.scroll-bottom {
  position: sticky; bottom: 8px; align-self: center; display: none; background: var(--bg-active);
  border: 1px solid var(--border); border-radius: 999px; padding: 4px 12px; font-size: 11px; color: #fff; z-index: 5;
}
.scroll-bottom.show { display: inline-flex; }

.ai-composer { border-top: 1px solid var(--border-soft); padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.ai-box {
  background: var(--composer); border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px;
  display: flex; flex-direction: column; gap: 8px;
}
.ai-box:focus-within { border-color: var(--accent); box-shadow: 0 0 0 1px rgba(129,161,193,0.25); }
#ai-input {
  width: 100%; min-height: 64px; max-height: 180px; background: transparent; border: none; outline: none;
  resize: none; font-size: 13px; line-height: 1.45; color: #fff;
}
.ai-box-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.ai-box-footer .left-meta { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.meta-pill {
  font-size: 11px; color: var(--fg-muted); background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 999px; padding: 2px 8px;
}
.ai-send {
  background: linear-gradient(180deg, #8fb4d4, #6f93b5); color: #0b0b0b; border-radius: 8px;
  padding: 6px 14px; font-size: 12px; font-weight: 600;
}
.ai-send:disabled { opacity: 0.45; cursor: default; }
.ai-hint { font-size: 11px; color: var(--fg-muted); text-align: center; }

/* Diffs view */
.diffs-view {
  display: none; flex: 1; flex-direction: column; background: var(--bg); min-width: 0;
}
.diffs-view.visible { display: flex; }
.diffs-h {
  height: 36px; display: flex; align-items: center; justify-content: space-between; padding: 0 12px;
  border-bottom: 1px solid var(--border-soft); font-size: 12px; color: var(--fg-dim);
}
.diffs-body { flex: 1; overflow: auto; padding: 12px; font-family: var(--font-mono); font-size: 12px; }
.diff-file { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; overflow: hidden; }
.diff-file .dh { background: var(--bg-elev); padding: 8px 10px; color: var(--accent-2); }
.diff-file .db { padding: 8px 10px; white-space: pre-wrap; color: var(--fg-dim); }
.diff-add { color: var(--green); } .diff-del { color: var(--red); }

/* Browser / Design Mode */
.browser-view {
  display: none; flex: 1; flex-direction: column; background: #080808; min-width: 0; border-left: 1px solid var(--border-soft);
}
.browser-view.visible { display: flex; }
.browser-bar {
  height: 36px; display: flex; align-items: center; gap: 8px; padding: 0 10px; border-bottom: 1px solid var(--border-soft);
}
.browser-bar input {
  flex: 1; background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; outline: none;
}
.browser-stage {
  flex: 1; margin: 10px; border: 1px dashed var(--border); border-radius: 10px; position: relative;
  background: linear-gradient(135deg, #101820, #0d0d0d); overflow: hidden;
}
.browser-stage.design { cursor: crosshair; }
.browser-stage .anno {
  position: absolute; border: 1.5px solid var(--accent-2); background: rgba(136,192,208,0.12); pointer-events: none;
}
.browser-hint { position: absolute; bottom: 12px; left: 12px; color: var(--fg-muted); font-size: 11px; }

/* Command palette + Cmd+K */
.overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none; align-items: flex-start;
  justify-content: center; padding-top: 12vh; z-index: 1000;
}
.overlay.show { display: flex; }
.palette, .inline-edit {
  width: min(560px, 92vw); background: #151515; border: 1px solid #333; border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.55); overflow: hidden;
}
.palette input, .inline-edit textarea {
  width: 100%; background: transparent; border: none; outline: none; padding: 14px 16px; font-size: 14px; color: #fff;
}
.palette-list { max-height: 320px; overflow: auto; border-top: 1px solid #222; }
.palette-item {
  padding: 10px 16px; display: flex; justify-content: space-between; color: var(--fg-dim); cursor: pointer;
}
.palette-item:hover, .palette-item.active { background: #1f1f1f; color: #fff; }
.palette-item .k { font-size: 11px; color: var(--fg-muted); font-family: var(--font-mono); }
.inline-edit .ie-foot {
  display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; border-top: 1px solid #222;
  color: var(--fg-muted); font-size: 11px;
}

.statusbar {
  height: var(--statusbar-h); background: var(--bg-status); color: var(--fg-dim);
  display: flex; align-items: center; justify-content: space-between; padding: 0 6px;
  font-size: 11px; border-top: 1px solid var(--border-soft); flex-shrink: 0;
}
.statusbar .left, .statusbar .right { display: flex; align-items: center; }
.statusbar .item { padding: 0 8px; height: 24px; display: flex; align-items: center; gap: 4px; border-radius: 4px; }
.statusbar .item:hover { background: var(--bg-hover); color: #fff; }
.resize-v { height: 3px; cursor: row-resize; flex-shrink: 0; }
.resize-v:hover { background: var(--accent); }
.resize-h { width: 3px; cursor: col-resize; flex-shrink: 0; }
.resize-h:hover { background: var(--accent); }
.toast {
  position: fixed; bottom: 40px; left: 50%; transform: translateX(-50%); background: #1a1a1a;
  border: 1px solid #333; color: #fff; padding: 8px 14px; border-radius: 8px; font-size: 12px; z-index: 1100; display: none;
}
.toast.show { display: block; animation: fade 2.4s forwards; }
@keyframes fade {
  0% { opacity: 0; transform: translateX(-50%) translateY(8px); }
  12% { opacity: 1; transform: translateX(-50%) translateY(0); }
  80% { opacity: 1; }
  100% { opacity: 0; }
}
.ctx-menu {
  position: fixed; z-index: 2000; min-width: 200px; background: #1a1a1a; border: 1px solid #333;
  border-radius: 8px; padding: 4px; box-shadow: 0 12px 40px rgba(0,0,0,0.55); display: none;
}
.ctx-menu.show { display: block; }
.ctx-menu button {
  display: flex; width: 100%; text-align: left; padding: 7px 10px; border-radius: 5px;
  font-size: 12px; color: var(--fg); gap: 8px; align-items: center;
}
.ctx-menu button:hover { background: #2a2a2a; }
.ctx-menu button.danger { color: var(--red); }
.ctx-menu .sep { height: 1px; background: #2a2a2a; margin: 4px 6px; }
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 2100; display: none;
  align-items: center; justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal {
  width: min(420px, 92vw); background: #151515; border: 1px solid #333; border-radius: 12px;
  padding: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.55);
}
.modal h3 { font-size: 14px; margin-bottom: 6px; color: #fff; font-weight: 600; }
.modal p { font-size: 12px; color: var(--fg-muted); margin-bottom: 12px; }
.modal input {
  width: 100%; background: #0f0f0f; border: 1px solid #333; border-radius: 8px;
  padding: 9px 10px; outline: none; font-size: 13px; color: #fff; margin-bottom: 14px;
}
.modal input:focus { border-color: var(--accent); }
.modal .row { display: flex; justify-content: flex-end; gap: 8px; }
.modal .row button { padding: 7px 12px; border-radius: 7px; font-size: 12px; }
.modal .row .cancel { color: var(--fg-dim); background: #222; }
.modal .row .ok { background: linear-gradient(180deg, #8fb4d4, #6f93b5); color: #0b0b0b; font-weight: 600; }
.sidebar-header .actions { display: flex; gap: 2px; }
.sidebar-header .actions button {
  width: 24px; height: 24px; border-radius: 5px; color: var(--fg-dim); font-size: 13px;
  display: flex; align-items: center; justify-content: center;
}
.sidebar-header .actions button:hover { background: var(--bg-hover); color: #fff; }

/* Cursor 3–imported Agents Window (Cats-branded) */
.agents-rail-h .new { color: var(--accent-2); font-size: 16px; width: 24px; height: 24px; border-radius: 6px; }
.agents-rail-h .new:hover { background: var(--bg-hover); }
.agent-section { padding: 8px 8px 4px; }
.agent-section .sec {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.7px; color: var(--fg-muted);
  font-weight: 600; padding: 4px 6px 6px; display: flex; justify-content: space-between;
}
.agent-card .actions { display: none; gap: 4px; margin-top: 8px; }
.agent-card.active .actions, .agent-card:hover .actions { display: flex; }
.agent-card .actions button {
  flex: 1; font-size: 10px; padding: 4px 6px; border-radius: 5px; background: #1a1a1a;
  border: 1px solid var(--border); color: var(--fg-dim);
}
.agent-card .actions button:hover { color: #fff; border-color: var(--accent); }
.tile-bar {
  display: none; align-items: center; gap: 6px; padding: 6px 12px;
  border-bottom: 1px solid var(--border-soft); background: #0e0e0e;
}
#app.agents-layout .tile-bar { display: flex; }
.tile-bar button {
  font-size: 11px; padding: 4px 8px; border-radius: 6px; color: var(--fg-dim); border: 1px solid var(--border);
}
.tile-bar button.active, .tile-bar button:hover { color: #fff; background: var(--bg-active); }
.agent-tiles {
  display: none; flex: 1; min-height: 0; gap: 1px; background: var(--border-soft);
}
.agent-tiles.on { display: flex; }
.agent-tiles .tile {
  flex: 1; min-width: 0; background: var(--bg-agents); display: flex; flex-direction: column;
  border-right: 1px solid var(--border-soft);
}
.agent-tiles .tile .tile-h {
  height: 32px; display: flex; align-items: center; justify-content: space-between;
  padding: 0 10px; border-bottom: 1px solid var(--border-soft); font-size: 12px; color: var(--fg-dim);
}
.agent-tiles .tile .tile-body { flex: 1; overflow: auto; padding: 12px; font-size: 12px; line-height: 1.5; }
.commit-bar {
  display: none; gap: 8px; align-items: center; padding: 10px 12px; border-top: 1px solid var(--border);
  background: #101010;
}
.diffs-view.visible .commit-bar { display: flex; }
.commit-bar input {
  flex: 1; background: #151515; border: 1px solid var(--border); border-radius: 7px; padding: 7px 10px; outline: none;
}
.commit-bar button {
  padding: 7px 10px; border-radius: 7px; font-size: 12px; border: 1px solid var(--border); color: var(--fg-dim);
}
.commit-bar button.primary { background: linear-gradient(180deg, #8fb4d4, #6f93b5); color: #0b0b0b; border: none; font-weight: 600; }
.workspace-pill {
  font-size: 11px; color: var(--fg-muted); padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border);
  max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.strip-btn {
  font-size: 11px; padding: 4px 10px; border-radius: 6px; color: var(--fg-dim);
  border: 1px solid var(--border); background: var(--bg); font-weight: 500;
  display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
}
.strip-btn:hover { color: #fff; border-color: var(--accent); background: var(--bg-hover); }
#app.agents-layout .ai-panel { position: relative; }
.agents-empty-hero {
  margin: auto; max-width: 420px; text-align: center; padding: 24px;
}
.agents-empty-hero h2 { font-size: 22px; color: #fff; font-weight: 600; margin-bottom: 8px; letter-spacing: -0.3px; }
.agents-empty-hero p { color: var(--fg-muted); font-size: 13px; line-height: 1.55; margin-bottom: 18px; }
.agents-empty-hero .starter {
  display: grid; gap: 8px; text-align: left;
}
.agents-empty-hero .starter button {
  padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border); background: #121212;
  color: var(--fg-dim); font-size: 12px;
}
.agents-empty-hero .starter button:hover { border-color: var(--accent); color: #fff; }
.agents-empty-hero .starter strong { color: #fff; display: block; margin-bottom: 2px; }

/* Cats Agents layout */
#app.agents-layout .sidebar,
#app.agents-layout .activity { display: none; }
#app.agents-layout #editor-column { display: none; }
#app.agents-layout #editor-column.show-diffs {
  display: flex; flex: 0 0 46%; min-width: 320px; border-right: 1px solid var(--border-soft);
}
#app.agents-layout #editor-column.show-diffs .editor-wrap,
#app.agents-layout #editor-column.show-diffs .tabs,
#app.agents-layout #editor-column.show-diffs .breadcrumb,
#app.agents-layout #editor-column.show-diffs .panel,
#app.agents-layout #editor-column.show-diffs #resize-panel { display: none !important; }
#app.agents-layout .agents-rail { display: flex; }
#app.agents-layout .ai-panel { flex: 1; width: auto; max-width: none; border-left: none; }
#app.agents-layout .ai-panel.tiles-on .ai-modes,
#app.agents-layout .ai-panel.tiles-on .ai-messages,
#app.agents-layout .ai-panel.tiles-on .ai-composer { display: none; }
#app.agents-layout .browser-view.agents-show { display: flex; width: 42%; }
#app.editor-layout .agents-rail { display: none; }
#app.editor-layout .tile-bar { display: none !important; }
</style>
</head>
<body>
<div id="app" class="agents-layout">
  <div class="topbar">
    <div class="brand"><em>Cats</em> <span>IDE 0.1</span></div>
    <button class="strip-btn" id="btn-open-folder" title="Open Folder">Open Folder</button>
    <span class="workspace-pill" id="workspace-pill" title="Current folder">workspace</span>
    <div class="layout-switch">
      <button id="btn-editor-layout">Editor</button>
      <button class="active" id="btn-agents-layout">Agents</button>
    </div>
    <div class="spacer"></div>
    <div class="env-chips">
      <span class="chip on" data-env="local">Local</span>
      <span class="chip" data-env="worktree">Worktree</span>
      <span class="chip" data-env="cloud">Cloud</span>
      <span class="chip" data-env="ssh">SSH</span>
    </div>
    <button class="icon-btn" id="btn-palette" title="Command Palette">⌘P</button>
    <button class="icon-btn" id="btn-inline" title="Inline Edit">⌘K</button>
  </div>

  <div class="menubar">
    <button data-menu="file">File</button>
    <button data-menu="edit">Edit</button>
    <button data-menu="selection">Selection</button>
    <button data-menu="view">View</button>
    <button data-menu="go">Go</button>
    <button data-menu="agent">Agent</button>
    <button data-menu="run">Run</button>
    <button data-menu="terminal">Terminal</button>
    <button data-menu="help">Help</button>
  </div>

  <div class="body">
    <nav class="activity" id="activity">
      <button class="act-btn active" data-view="explorer" title="Explorer">
        <svg viewBox="0 0 24 24"><path d="M3 3h8v8H3V3zm10 0h8v8h-8V3zM3 13h8v8H3v-8zm10 3h8v5h-8v-5z"/></svg>
      </button>
      <button class="act-btn" data-view="search" title="Search">
        <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.5 6.5 0 1 0 14 15.5l.27.28v.79l5 5L20.49 19l-5-5zm-6 0A4.5 4.5 0 1 1 14 9.5 4.5 4.5 0 0 1 9.5 14z"/></svg>
      </button>
      <button class="act-btn" data-view="git" title="Source Control">
        <svg viewBox="0 0 24 24"><path d="M6 3a3 3 0 0 1 2.8 4H11a2 2 0 0 1 2 2v3.2A3 3 0 1 1 11 15v-4H8.8A3 3 0 1 1 6 3zm0 2a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm10 10a1 1 0 1 0 0 2 1 1 0 0 0 0-2z"/></svg>
      </button>
      <button class="act-btn" data-view="run" title="Run and Debug">
        <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7L8 5z"/></svg>
      </button>
      <button class="act-btn" data-view="ext" title="Cats Marketplace">
        <svg viewBox="0 0 24 24"><path d="M4 4h7v7H4V4zm9 0h7v7h-7V4zM4 13h7v7H4v-7zm11 0h3v3h-3v-3zm0 5h3v2h-3v-2zm5-5h2v7h-2v-7z"/></svg>
      </button>
      <button class="act-btn" data-view="diffs" title="Diffs Review">
        <svg viewBox="0 0 24 24"><path d="M4 4h7v16H4V4zm9 0h7v7h-7V4zm0 9h7v7h-7v-7z"/></svg>
      </button>
      <div class="spacer"></div>
      <button class="act-btn" id="btn-ai-toggle" title="Agent Panel" style="color:#fff">
        <svg viewBox="0 0 24 24"><path d="M12 3a7 7 0 0 0-7 7c0 2.8 1.6 5.2 4 6.3V20l3-1.5c.3.1.7.1 1 .1a7 7 0 0 0 0-14zm0 12.5c-.3 0-.6 0-.9-.1l-1.6.8v-1.6A5 5 0 1 1 12 15.5z"/></svg>
      </button>
      <button class="act-btn" id="btn-browser" title="Browser / Design Mode">
        <svg viewBox="0 0 24 24"><path d="M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm1 3v9h14V8H5zm2-2h2v1H7V6z"/></svg>
      </button>
      <button class="act-btn" data-view="settings" title="Settings" id="btn-settings">
        <svg viewBox="0 0 24 24"><path d="M19.1 12.9a7.4 7.4 0 0 0 0-1.8l2-1.5-2-3.4-2.4 1a7 7 0 0 0-1.6-.9l-.4-2.5H9.7l-.4 2.5a7 7 0 0 0-1.6.9l-2.4-1-2 3.4 2 1.5a7.4 7.4 0 0 0 0 1.8l-2 1.5 2 3.4 2.4-1c.5.4 1 .7 1.6.9l.4 2.5h3.8l.4-2.5c.6-.2 1.1-.5 1.6-.9l2.4 1 2-3.4-2-1.5zM12 15.5A3.5 3.5 0 1 1 12 8a3.5 3.5 0 0 1 0 7.5z"/></svg>
      </button>
    </nav>

    <aside class="agents-rail" id="agents-rail">
      <div class="agents-rail-h">
        <span>Cats Agents</span>
        <div style="display:flex;gap:4px;align-items:center">
          <button class="new" id="btn-agents-settings" title="Settings · LM Studio">⚙</button>
          <button class="new" id="btn-new-agent" title="New Agent">+</button>
        </div>
      </div>
      <div class="agent-list" id="agent-list"></div>
    </aside>

    <aside class="sidebar" id="sidebar">
      <div class="sidebar-header">
        <span id="sidebar-title">Explorer</span>
        <div class="actions">
          <button title="New File" id="btn-new-file">＋</button>
          <button title="New Folder" id="btn-new-folder">▣</button>
          <button title="Refresh" id="btn-refresh">↻</button>
        </div>
      </div>
      <div class="sidebar-body" id="explorer-body"></div>
      <div class="search-panel" id="search-panel">
        <input id="search-input" placeholder="Search files" />
        <div class="search-results" id="search-results">Type to search</div>
      </div>
    </aside>
    <div class="resize-h" id="resize-sidebar"></div>

    <main class="center" id="editor-column">
      <div class="tabs" id="tabs"></div>
      <div class="breadcrumb" id="breadcrumb"><span>Welcome</span></div>
      <div class="editor-wrap" id="editor-wrap">
        <div class="gutter" id="gutter"></div>
        <div class="editor-area" style="flex:1;position:relative;min-width:0">
          <textarea id="code" spellcheck="false" hidden></textarea>
          <div class="welcome" id="welcome">
            <h1><em>Cats</em></h1>
            <div class="sub">CatIDE 0.1 · Cats Agents · Design Mode · Diffs</div>
            <div class="actions">
              <button id="welcome-folder">Open Folder…</button>
              <button id="welcome-open">Open File…</button>
              <button id="welcome-new">New Untitled File</button>
              <button id="welcome-agents">Open Cats Agents</button>
              <button id="welcome-ai">New Agent Chat</button>
            </div>
            <div class="keys">
              <span><kbd>⌘P</kbd> Commands</span>
              <span><kbd>⌘K</kbd> Inline Edit</span>
              <span><kbd>⌘L</kbd> Agent</span>
              <span><kbd>⌘⇧A</kbd> Cats Agents</span>
            </div>
          </div>
        </div>
      </div>
      <div class="diffs-view" id="diffs-view">
        <div class="diffs-h">
          <span>Review Diffs</span>
          <div style="display:flex;gap:8px;align-items:center">
            <button id="btn-stage-all" style="color:var(--accent-2)">Stage All</button>
            <button id="btn-close-diffs" style="color:var(--fg-dim)">Close</button>
          </div>
        </div>
        <div class="diffs-body" id="diffs-body">No agent changes yet.</div>
        <div class="commit-bar">
          <input id="commit-msg" placeholder="Commit message" />
          <button id="btn-commit">Commit</button>
          <button class="primary" id="btn-pr">Create PR</button>
        </div>
      </div>
      <div class="resize-v" id="resize-panel"></div>
      <div class="panel" id="panel">
        <div class="panel-tabs">
          <button class="ptab active" data-panel="problems">Problems</button>
          <button class="ptab" data-panel="output">Output</button>
          <button class="ptab" data-panel="debug">Debug Console</button>
          <button class="ptab" data-panel="terminal">Terminal</button>
        </div>
        <div class="panel-body">
          <div id="problems">No problems have been detected in the workspace.</div>
          <div id="output" hidden></div>
          <div id="debug" hidden></div>
          <div id="terminal" hidden><span style="color:var(--green)">cat@cats</span> Cats terminal ready.
</div>
        </div>
        <div class="term-input-row" id="term-row" hidden>
          <span class="prompt">$</span>
          <input id="term-input" autocomplete="off" spellcheck="false" />
        </div>
      </div>
    </main>

    <div class="resize-h" id="resize-ai"></div>
    <aside class="ai-panel" id="ai-panel">
      <div class="ai-header">
        <div class="ai-tabs" id="ai-tabs"></div>
        <button id="ai-clear" style="color:var(--fg-dim)">Clear</button>
      </div>
      <div class="tile-bar" id="tile-bar">
        <button class="active" id="btn-tile-single">Single</button>
        <button id="btn-tile-split">Split</button>
        <button id="btn-tile-grid">Grid</button>
        <span style="flex:1"></span>
        <button id="btn-handoff-cloud">↗ Cloud</button>
        <button id="btn-handoff-local">↙ Local</button>
      </div>
      <div class="agent-tiles" id="agent-tiles"></div>
      <div class="ai-modes" id="ai-modes">
        <button class="active" data-mode="agent">Agent</button>
        <button data-mode="ask">Ask</button>
        <button data-mode="edit">Edit</button>
        <button data-mode="plan">Plan</button>
        <button data-mode="manual">Manual</button>
      </div>
      <div class="ai-messages" id="ai-messages">
        <div class="ai-empty agents-empty-hero" id="ai-empty">
          <h2>Build with Cats Agents</h2>
          <p>Unified agent workspace — local, cloud, worktrees, and SSH — with the depth of an IDE when you need it.</p>
          <div class="starter">
            <button data-starter="Fix bugs in this repo and add tests."><strong>Fix &amp; test</strong>Agent explores and patches</button>
            <button data-starter="Explain the architecture of this project."><strong>Understand codebase</strong>Ask mode overview</button>
            <button data-starter="/best-of-n Implement a small improvement and compare approaches."><strong>Best of N</strong>Parallel agent comparison</button>
            <button data-starter="/worktree Create an isolated change for a safe experiment."><strong>Worktree</strong>Isolated agent checkout</button>
          </div>
        </div>
        <button class="scroll-bottom" id="scroll-bottom">↓ Latest</button>
      </div>
      <div class="ai-composer">
        <div class="ai-box">
          <textarea id="ai-input" placeholder="Plan, search, build anything…  /worktree  /best-of-n  @files" rows="3"></textarea>
          <div class="ai-box-footer">
            <div class="left-meta">
              <span class="meta-pill" id="ai-mode-label">Agent · tools on</span>
              <span class="meta-pill" id="ai-env-label">Local</span>
              <span class="meta-pill">Cats AI</span>
            </div>
            <button class="ai-send" id="ai-send">Send</button>
          </div>
        </div>
        <div class="ai-hint" id="ai-hint">Cats AI · auto-detect LM Studio / Ollama · Puter · keys · Enter send</div>
      </div>
    </aside>

    <div class="browser-view" id="browser-view">
      <div class="browser-bar">
        <button id="btn-design-mode" class="meta-pill">Design Mode</button>
        <input id="browser-url" value="http://127.0.0.1/" />
        <button id="btn-browser-close" style="color:var(--fg-dim)">✕</button>
      </div>
      <div class="browser-stage" id="browser-stage">
        <div class="browser-hint">Browser preview · ⌘⇧D Design Mode · Shift-drag to annotate · ⌘L add to chat</div>
      </div>
    </div>
  </div>

  <div class="statusbar">
    <div class="left">
      <div class="item" id="status-branch">⎇ main</div>
      <div class="item" id="status-layout">Editor</div>
      <div class="item" id="status-errors">0 ⚠  0 ✖</div>
    </div>
    <div class="right">
      <div class="item" id="status-cursor">Ln 1, Col 1</div>
      <div class="item">Spaces: 4</div>
      <div class="item">UTF-8</div>
      <div class="item" id="status-lang">Plain Text</div>
      <div class="item">Cats</div>
      <div class="item">CatIDE0.1</div>
    </div>
  </div>
</div>

<div class="overlay" id="palette-overlay">
  <div class="palette">
    <input id="palette-input" placeholder="Type a command or search files…" />
    <div class="palette-list" id="palette-list"></div>
  </div>
</div>
<div class="overlay" id="inline-overlay">
  <div class="inline-edit">
    <textarea id="inline-input" rows="3" placeholder="Edit selection or generate code… (⌘K)"></textarea>
    <div class="ie-foot"><span>Inline Edit · Edit mode agent</span><button class="ai-send" id="inline-send">Apply</button></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="ctx-menu" id="ctx-menu"></div>
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <h3 id="modal-title">New File</h3>
    <p id="modal-hint">Enter a name relative to the workspace</p>
    <input id="modal-input" spellcheck="false" autocomplete="off" />
    <div class="row">
      <button class="cancel" id="modal-cancel">Cancel</button>
      <button class="ok" id="modal-ok">Create</button>
    </div>
  </div>
</div>
<input type="file" id="file-picker" multiple hidden />

<script src="https://js.puter.com/v2/"></script>
<script>
const state = {
  files: [], openTabs: [], activePath: null, contents: {}, dirty: {},
  aiMode: "agent", aiBusy: false, view: "explorer", layout: "agents",
  env: "local", designMode: false, browserOpen: false, diffs: [],
  agents: [
    { id: "a1", title: "Cats Agent", env: "local", messages: [] },
    { id: "a2", title: "Cloud draft", env: "cloud", messages: [] },
  ],
  activeAgentId: "a1",
  ctxPath: "", ctxType: "dir",
  tileMode: "single",
  staged: false,
};
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
function ensureAgents() {
  if (!Array.isArray(state.agents)) state.agents = [];
  state.agents = state.agents.filter((a) => a && typeof a === "object");
  state.agents.forEach((a) => {
    if (!a.id) a.id = "a" + Math.floor(Math.random() * 1e7);
    if (!a.title) a.title = "Cats Agent";
    if (!a.env) a.env = state.env || "local";
    if (!Array.isArray(a.messages)) a.messages = [];
  });
  if (!state.agents.length) {
    state.agents.push({ id: "a1", title: "Cats Agent", env: state.env || "local", messages: [] });
  }
  if (!state.agents.some((a) => a.id === state.activeAgentId)) {
    state.activeAgentId = state.agents[0].id;
  }
  return state.agents;
}
const activeAgent = () => {
  ensureAgents();
  return state.agents.find((a) => a.id === state.activeAgentId) || state.agents[0];
};

function toast(msg) {
  const t = $("#toast"); if (!t) return;
  t.textContent = msg; t.classList.remove("show"); void t.offsetWidth; t.classList.add("show");
}
function langFor(path) {
  const ext = (path || "").split(".").pop().toLowerCase();
  const map = { py:"Python", js:"JavaScript", ts:"TypeScript", tsx:"TSX", html:"HTML", css:"CSS", json:"JSON", md:"Markdown", rs:"Rust", go:"Go", sh:"Shell" };
  return map[ext] || "Plain Text";
}
function iconFor(name, isDir) {
  if (isDir) return "📁";
  const ext = name.split(".").pop().toLowerCase();
  return ({ py:"🐍", js:"📜", ts:"💙", html:"🌐", css:"🎨", json:"{}", md:"📝", rs:"🦀" })[ext] || "📄";
}
async function api(path, opts={}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json", ...(opts.headers||{}) }, ...opts });
  const ct = res.headers.get("content-type") || "";
  if (!res.ok) {
    const raw = await res.text();
    try {
      const j = JSON.parse(raw);
      throw new Error(j.error || raw || res.statusText);
    } catch (e) {
      if (e instanceof Error && e.message && e.message !== raw) throw e;
      throw new Error(raw || res.statusText);
    }
  }
  return ct.includes("application/json") ? res.json() : res.text();
}
function modeLabel() {
  return ({ agent:"Agent · tools on", ask:"Ask · read-only", edit:"Edit · file changes", plan:"Plan · outline first", manual:"Manual · guided" })[state.aiMode] || "Agent";
}
function resetWorkspaceUI(name) {
  state.openTabs = [];
  state.activePath = null;
  state.contents = {};
  state.dirty = {};
  state.diffs = [];
  state.staged = false;
  showEditor(false);
  renderTabs();
  updateBreadcrumb(null);
  const pill = $("#workspace-pill");
  if (pill) { pill.textContent = name || "workspace"; pill.title = name || ""; }
  toast("Opened folder · " + (name || "workspace"));
}
async function openFolder() {
  let path = null;
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_folder) {
      path = await window.pywebview.api.open_folder();
    }
  } catch (_) {}
  if (!path) {
    path = await askModal({
      title: "Open Folder",
      hint: "Absolute path to a folder on disk",
      value: "",
      okLabel: "Open",
    });
  }
  if (!path) return;
  path = String(path).trim();
  if (!path) return;
  try {
    const data = await api("/api/open_folder", { method: "POST", body: JSON.stringify({ path }) });
    resetWorkspaceUI(data.name || data.path);
    await loadTree();
    setView("explorer");
    if (state.layout === "agents") {
      // keep agents window; explorer updates for when user switches to Editor
    } else {
      setLayout("editor");
    }
  } catch (e) {
    toast("Open Folder failed: " + e.message);
  }
}
function setLayout(wh) {
  state.layout = wh;
  $("#app").classList.toggle("agents-layout", wh === "agents");
  $("#app").classList.toggle("editor-layout", wh === "editor");
  $("#btn-editor-layout").classList.toggle("active", wh === "editor");
  $("#btn-agents-layout").classList.toggle("active", wh === "agents");
  $("#status-layout").textContent = wh === "agents" ? "Cats Agents" : "Cats Editor";
  if (wh === "agents") {
    $("#ai-panel").classList.remove("hidden");
    $("#agents-rail").classList.add("visible");
  } else {
    $("#editor-column").classList.remove("show-diffs");
    setTileMode("single");
  }
}
function handoffAgent(toEnv) {
  const a = activeAgent();
  if (!a || !toEnv) return;
  a.env = toEnv;
  state.env = toEnv;
  $$(".env-chips .chip").forEach((c) => c.classList.toggle("on", c.dataset.env === toEnv));
  const lab = $("#ai-env-label");
  if (lab) lab.textContent = toEnv.charAt(0).toUpperCase() + toEnv.slice(1);
  renderAgentsRail();
  toast(toEnv === "cloud" ? "Handed off to Cloud agent" : "Brought agent Local for fast iteration");
}
function setTileMode(mode) {
  ensureAgents();
  state.tileMode = mode || "single";
  ["single", "split", "grid"].forEach((m) => {
    const b = $("#btn-tile-" + m);
    if (b) b.classList.toggle("active", m === state.tileMode);
  });
  const tiles = $("#agent-tiles");
  const panel = $("#ai-panel");
  if (!tiles || !panel) return;
  if (state.tileMode === "single") {
    tiles.classList.remove("on");
    panel.classList.remove("tiles-on");
    tiles.innerHTML = "";
    return;
  }
  panel.classList.add("tiles-on");
  tiles.classList.add("on");
  tiles.style.flexWrap = state.tileMode === "grid" ? "wrap" : "nowrap";
  tiles.innerHTML = "";
  // Ensure enough agents for split/grid without out-of-range access
  const need = state.tileMode === "split" ? 2 : Math.min(4, Math.max(2, state.agents.length));
  while (state.agents.length < need) newAgent(true);
  const list = state.agents.slice(0, state.tileMode === "split" ? 2 : 4);
  list.forEach((a) => {
    if (!a) return;
    const tile = document.createElement("div");
    tile.className = "tile";
    if (state.tileMode === "grid") tile.style.flex = "1 1 48%";
    const msgs = Array.isArray(a.messages) ? a.messages : [];
    const last = [...msgs].reverse().find((m) => m && !m.thinking) || { content: "No messages yet — start chatting in Single view." };
    tile.innerHTML = `<div class="tile-h"><span>${a.title || "Agent"}</span><span class="workspace-pill">${a.env || "local"}</span></div>
      <div class="tile-body">${renderMarkdownLite(String(last.content || "").slice(0, 900))}</div>`;
    tile.onclick = () => {
      state.activeAgentId = a.id;
      setTileMode("single");
      renderAgentsRail(); renderAiTabs(); renderMessages();
    };
    tiles.appendChild(tile);
  });
}
function renderAgentsRail() {
  const list = $("#agent-list"); if (!list) return;
  ensureAgents();
  list.innerHTML = "";
  const groups = [
    { key: "local", label: "Local" },
    { key: "cloud", label: "Cloud" },
    { key: "worktree", label: "Worktrees" },
    { key: "ssh", label: "SSH" },
  ];
  groups.forEach((g) => {
    const agents = state.agents.filter((a) => (a.env || "local") === g.key);
    const sec = document.createElement("div");
    sec.className = "agent-section";
    sec.innerHTML = `<div class="sec"><span>${g.label}</span><span>${agents.length}</span></div>`;
    list.appendChild(sec);
    if (!agents.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "padding:4px 10px 10px;color:var(--fg-muted);font-size:11px";
      empty.textContent = "No agents";
      list.appendChild(empty);
      return;
    }
    agents.forEach((a) => {
      // div (not button) — nested buttons broke the agent list in WebKit
      const el = document.createElement("div");
      el.className = "agent-card" + (a.id === state.activeAgentId ? " active" : "");
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      el.innerHTML = `<div class="t">${a.title || "Agent"}</div>
        <div class="m"><span class="dot ${a.env==="cloud"?"cloud":a.env==="local"?"":"idle"}"></span>${a.env || "local"} · ${(a.messages||[]).length} msgs</div>
        <div class="actions">
          <button type="button" data-act="local">↙ Local</button>
          <button type="button" data-act="cloud">↗ Cloud</button>
        </div>`;
      el.onclick = () => {
        state.activeAgentId = a.id;
        state.env = a.env || "local";
        renderAgentsRail(); renderAiTabs(); renderMessages();
        if (state.tileMode && state.tileMode !== "single") setTileMode(state.tileMode);
      };
      el.querySelectorAll(".actions button").forEach((b) => {
        b.onclick = (e) => {
          e.stopPropagation();
          state.activeAgentId = a.id;
          handoffAgent(b.dataset.act);
        };
      });
      list.appendChild(el);
    });
  });
}
function renderAiTabs() {
  const tabs = $("#ai-tabs"); if (!tabs) return;
  ensureAgents();
  tabs.innerHTML = "";
  state.agents.forEach((a) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ai-tab" + (a.id === state.activeAgentId ? " active" : "");
    b.textContent = a.title || "Agent";
    b.onclick = () => { state.activeAgentId = a.id; renderAgentsRail(); renderAiTabs(); renderMessages(); };
    tabs.appendChild(b);
  });
  const plus = document.createElement("button");
  plus.type = "button";
  plus.className = "ai-tab"; plus.textContent = "+";
  plus.onclick = () => newAgent(); tabs.appendChild(plus);
}
function newAgent(silent) {
  ensureAgents();
  const id = "a" + (Date.now() % 1e7);
  const n = state.agents.length + 1;
  state.agents.push({ id, title: "Cats Agent " + n, env: state.env || "local", messages: [] });
  state.activeAgentId = id;
  renderAgentsRail(); renderAiTabs(); renderMessages();
  if (silent !== true) toast("New Cats agent");
}
async function loadTree() {
  try { state.files = await api("/api/tree"); renderTree(); } catch (e) { toast("Tree failed: " + e.message); }
}
function renderTree() {
  const body = $("#explorer-body");
  if (state.view !== "explorer") return;
  body.innerHTML = "";
  const root = document.createElement("div");
  root.className = "tree-item tree-folder selected";
  root.innerHTML = `<span class="chev">▾</span><span class="icon">📂</span><span class="name">${state.files.name || "workspace"}</span>`;
  root.oncontextmenu = (e) => {
    e.preventDefault(); e.stopPropagation();
    showCtx(e.clientX, e.clientY, explorerCtxItems(".", "dir"));
  };
  body.appendChild(root);
  function walk(nodes, depth) {
    (nodes || []).forEach((n) => {
      const el = document.createElement("div");
      el.className = `tree-item ${n.type === "dir" ? "tree-folder" : "tree-file"} indent-${Math.min(depth,4)}`;
      el.innerHTML = `<span class="chev">${n.type==="dir"?"▸":""}</span><span class="icon">${iconFor(n.name, n.type==="dir")}</span><span class="name">${n.name}</span>`;
      el.oncontextmenu = (e) => {
        e.preventDefault(); e.stopPropagation();
        showCtx(e.clientX, e.clientY, explorerCtxItems(n.path, n.type));
      };
      if (n.type === "file") el.onclick = () => openFile(n.path);
      else {
        el.onclick = (e) => {
          e.stopPropagation();
          const open = el.dataset.open === "1"; el.dataset.open = open ? "0" : "1";
          el.querySelector(".chev").textContent = open ? "▸" : "▾";
          let sib = el.nextElementSibling;
          while (sib && parseInt((sib.className.match(/indent-(\d)/)||[])[1]||"0") > depth) {
            sib.style.display = open ? "none" : ""; sib = sib.nextElementSibling;
          }
        };
      }
      if (state.activePath === n.path) el.classList.add("selected");
      body.appendChild(el);
      if (n.type === "dir" && n.children) walk(n.children, depth + 1);
    });
  }
  walk(state.files.children || [], 1);
  body.oncontextmenu = (e) => {
    if (e.target === body) {
      e.preventDefault();
      showCtx(e.clientX, e.clientY, explorerCtxItems(".", "dir"));
    }
  };
}
function renderTabs() {
  const tabs = $("#tabs"); tabs.innerHTML = "";
  state.openTabs.forEach((path) => {
    const name = path.split("/").pop();
    const tab = document.createElement("div");
    tab.className = "tab" + (path === state.activePath ? " active" : "");
    const dirty = state.dirty[path] ? "●" : "";
    tab.innerHTML = `<span>${iconFor(name,false)}</span><span style="overflow:hidden;text-overflow:ellipsis">${name}</span><span>${dirty}</span><button class="close">×</button>`;
    tab.onclick = (e) => { if (e.target.classList.contains("close")) { e.stopPropagation(); closeTab(path); } else switchTab(path); };
    tabs.appendChild(tab);
  });
}
function updateBreadcrumb(path) {
  const bc = $("#breadcrumb");
  if (!path) { bc.innerHTML = "<span>Welcome</span>"; return; }
  bc.innerHTML = path.split("/").map((p,i,a)=>`<span>${p}</span>${i<a.length-1?'<span class="sep"> › </span>':''}`).join("");
}
function updateStatus() {
  const code = $("#code"); const text = code.value || ""; const pos = code.selectionStart || 0;
  const before = text.slice(0, pos); const lines = before.split("\n");
  $("#status-cursor").textContent = `Ln ${lines.length}, Col ${lines[lines.length-1].length+1}`;
  $("#status-lang").textContent = langFor(state.activePath);
  const gut = $("#gutter"); const total = text.split("\n").length || 1;
  gut.innerHTML = Array.from({length:total},(_,i)=>i+1).join("\n"); gut.scrollTop = code.scrollTop;
}
function showEditor(show) {
  $("#welcome").classList.toggle("hidden", show); $("#code").hidden = !show;
  $("#gutter").style.visibility = show ? "visible" : "hidden";
  $("#diffs-view").classList.remove("visible"); $("#editor-wrap").style.display = "";
}
async function openFile(path) {
  try {
    if (!state.contents[path]) state.contents[path] = (await api("/api/read?path="+encodeURIComponent(path))).content;
    if (!state.openTabs.includes(path)) state.openTabs.push(path);
    switchTab(path);
  } catch (e) { toast("Open failed: " + e.message); }
}
function switchTab(path) {
  if (state.activePath && state.contents[state.activePath] !== undefined) state.contents[state.activePath] = $("#code").value;
  state.activePath = path; $("#code").value = state.contents[path] ?? "";
  showEditor(true); renderTabs(); renderTree(); updateBreadcrumb(path); updateStatus(); $("#code").focus();
}
function closeTab(path) {
  const idx = state.openTabs.indexOf(path); if (idx < 0) return;
  state.openTabs.splice(idx,1); delete state.contents[path]; delete state.dirty[path];
  if (state.activePath === path) {
    state.activePath = state.openTabs[Math.max(0, idx-1)] || null;
    if (state.activePath) switchTab(state.activePath); else { showEditor(false); renderTabs(); updateBreadcrumb(null); }
  } else renderTabs();
}
let modalResolve = null;
function hideModal() {
  $("#modal-overlay").classList.remove("show");
  if (modalResolve) { const r = modalResolve; modalResolve = null; r(null); }
}
function askModal({ title, hint, value, okLabel }) {
  return new Promise((resolve) => {
    modalResolve = resolve;
    $("#modal-title").textContent = title || "Name";
    $("#modal-hint").textContent = hint || "";
    $("#modal-ok").textContent = okLabel || "OK";
    const inp = $("#modal-input");
    inp.value = value || "";
    $("#modal-overlay").classList.add("show");
    setTimeout(() => { inp.focus(); inp.select(); }, 30);
  });
}
function joinPath(dir, name) {
  const d = (dir || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const n = (name || "").replace(/\\/g, "/").replace(/^\/+/, "");
  if (!d || d === ".") return n;
  return d + "/" + n;
}
function parentDir(path) {
  if (!path || path === "." || path.startsWith("Untitled-")) return "";
  const parts = path.replace(/\\/g, "/").split("/");
  parts.pop();
  return parts.join("/");
}
async function createFileOnDisk(relPath, content) {
  const path = (relPath || "").trim().replace(/\\/g, "/");
  if (!path || path.startsWith("Untitled-")) throw new Error("Invalid file name");
  if (path.includes("..")) throw new Error("Invalid path");
  await api("/api/write", { method: "POST", body: JSON.stringify({ path, content: content ?? "" }) });
  state.contents[path] = content ?? "";
  if (!state.openTabs.includes(path)) state.openTabs.push(path);
  state.dirty[path] = false;
  await loadTree();
  switchTab(path);
  toast("Created " + path);
  return path;
}
async function createFolderOnDisk(relPath) {
  const path = (relPath || "").trim().replace(/\\/g, "/");
  if (!path) throw new Error("Invalid folder name");
  if (path.includes("..")) throw new Error("Invalid path");
  await api("/api/mkdir", { method: "POST", body: JSON.stringify({ path }) });
  await loadTree();
  setView("explorer");
  toast("Created folder " + path);
}
async function newUntitled() {
  // Prefer real file creation (prompt() is broken in pywebview)
  const dir = state.ctxPath && state.ctxType === "dir" ? state.ctxPath : parentDir(state.activePath);
  const name = await askModal({
    title: "New File",
    hint: dir ? `Creating in ${dir}/` : "Creating in workspace root",
    value: "untitled.py",
    okLabel: "Create",
  });
  if (!name) return;
  try { await createFileOnDisk(joinPath(dir, name), ""); }
  catch (e) { toast("Could not create file: " + e.message); }
}
async function newFolder() {
  const dir = state.ctxPath && state.ctxType === "dir" ? state.ctxPath : parentDir(state.activePath);
  const name = await askModal({
    title: "New Folder",
    hint: dir ? `Creating in ${dir}/` : "Creating in workspace root",
    value: "new-folder",
    okLabel: "Create",
  });
  if (!name) return;
  try { await createFolderOnDisk(joinPath(dir, name)); }
  catch (e) { toast("Could not create folder: " + e.message); }
}
async function saveActive() {
  if (!state.activePath) return;
  const content = $("#code").value; state.contents[state.activePath] = content;
  let path = state.activePath;
  if (path.startsWith("Untitled-")) {
    const name = await askModal({
      title: "Save As",
      hint: "Choose a file name in the workspace",
      value: path.replace("Untitled-", "untitled") + ".txt",
      okLabel: "Save",
    });
    if (!name) return;
    path = name.trim().replace(/\\/g, "/");
  }
  try {
    await api("/api/write", { method:"POST", body: JSON.stringify({ path, content }) });
    if (path !== state.activePath) {
      const old = state.activePath; state.openTabs = state.openTabs.map(p=>p===old?path:p);
      state.contents[path]=content; delete state.contents[old]; delete state.dirty[old]; state.activePath=path;
    }
    state.dirty[path]=false; await loadTree(); switchTab(path); toast("Saved " + path);
  } catch (e) { toast("Save failed: " + e.message); }
}
function hideCtx() { $("#ctx-menu").classList.remove("show"); }
function showCtx(x, y, items) {
  const menu = $("#ctx-menu");
  menu.innerHTML = "";
  items.forEach((it) => {
    if (it === "-") { const s = document.createElement("div"); s.className = "sep"; menu.appendChild(s); return; }
    const b = document.createElement("button");
    if (it.danger) b.classList.add("danger");
    b.textContent = it.label;
    b.onclick = async (e) => { e.stopPropagation(); hideCtx(); try { await it.run(); } catch (err) { toast(err.message); } };
    menu.appendChild(b);
  });
  menu.classList.add("show");
  const pad = 8;
  const mw = menu.offsetWidth || 200, mh = menu.offsetHeight || 180;
  menu.style.left = Math.min(x, window.innerWidth - mw - pad) + "px";
  menu.style.top = Math.min(y, window.innerHeight - mh - pad) + "px";
}
function explorerCtxItems(path, type) {
  state.ctxPath = path; state.ctxType = type;
  const items = [
    { label: "New File…", run: newUntitled },
    { label: "New Folder…", run: newFolder },
    "-",
    { label: "Open", run: async () => { if (type === "file") await openFile(path); } },
    { label: "Reveal in Explorer", run: async () => { setView("explorer"); toast(path || "workspace"); } },
    "-",
    { label: "Rename…", run: () => renamePath(path, type) },
    { label: "Delete…", danger: true, run: () => deletePath(path) },
    "-",
    { label: "Copy Path", run: async () => { try { await navigator.clipboard.writeText(path || "."); toast("Copied path"); } catch(_) { toast(path || "."); } } },
    { label: "Ask Agent about this", run: async () => {
      $("#ai-panel").classList.remove("hidden");
      $("#ai-input").value = type === "file" ? `Explain ${path}` : `What's in folder ${path || "."}?`;
      $("#ai-input").focus();
    }},
  ];
  return items;
}
async function renamePath(path, type) {
  if (!path || path === "." || path.startsWith("Untitled-")) { toast("Can't rename this"); return; }
  const base = path.split("/").pop();
  const name = await askModal({
    title: "Rename",
    hint: `Renaming ${path}`,
    value: base,
    okLabel: "Rename",
  });
  if (!name || name === base) return;
  const dest = joinPath(parentDir(path), name.trim());
  try {
    await api("/api/rename", { method: "POST", body: JSON.stringify({ path, dest }) });
    if (state.contents[path] != null) {
      state.contents[dest] = state.contents[path];
      delete state.contents[path];
      state.openTabs = state.openTabs.map((p) => (p === path ? dest : p));
      if (state.activePath === path) state.activePath = dest;
    }
    await loadTree();
    if (type === "file") switchTab(dest);
    toast("Renamed to " + dest);
  } catch (e) { toast("Rename failed: " + e.message); }
}
async function deletePath(path) {
  if (!path || path === "." || path.startsWith("Untitled-")) { toast("Can't delete this"); return; }
  const ok = await askModal({
    title: "Delete",
    hint: `Type DELETE to remove ${path}`,
    value: "",
    okLabel: "Delete",
  });
  if ((ok || "").trim().toUpperCase() !== "DELETE") { toast("Delete cancelled"); return; }
  try {
    await api("/api/delete", { method: "POST", body: JSON.stringify({ path }) });
    if (state.openTabs.includes(path)) closeTab(path);
    delete state.contents[path];
    await loadTree();
    toast("Deleted " + path);
  } catch (e) { toast("Delete failed: " + e.message); }
}
function openSettings() {
  if (state.layout !== "editor") setLayout("editor");
  $("#sidebar").classList.remove("hidden");
  setView("settings");
}
function renderSettings(status) {
  const body = $("#explorer-body");
  if (!body) return;
  const lmOn = !!(status && status.lmstudio);
  const olOn = !!(status && status.ollama);
  const active = (status && status.active_local) || null;
  const lmModels = (status && status.lmstudio_models) || [];
  const olModels = (status && status.ollama_models) || [];
  const keyed = (status && status.keyed_providers) || [];
  const esc = (s) => String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const modelList = (arr) => arr.length
    ? `<ul class="settings-list">${arr.slice(0, 10).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`
    : `<div class="settings-empty">No models listed — load one in the app, then re-check.</div>`;
  body.innerHTML = `
    <div class="settings-pane">
      <div class="sec-title">Local LLMs</div>
      <div class="settings-card">
        <div class="settings-row">
          <div>
            <div class="label">LM Studio</div>
            <div class="sub">OpenAI-compatible server · default <span class="settings-mono">127.0.0.1:1234</span></div>
          </div>
          <span class="settings-badge ${lmOn ? "on" : "off"}" id="set-lm-badge">${lmOn ? "Detected" : "Not found"}</span>
        </div>
        <div class="settings-mono" id="set-lm-detail">${lmOn
          ? ("Model: " + esc(status.lmstudio_model || "(none selected)") + (status.local_backends && status.local_backends.find(b=>b.name==="lmstudio") ? " · " + esc((status.local_backends.find(b=>b.name==="lmstudio")||{}).base || "") : ""))
          : "Start LM Studio → Local Server → Start Server, then Re-check."}</div>
        ${lmOn ? modelList(lmModels) : ""}
      </div>
      <div class="settings-card">
        <div class="settings-row">
          <div>
            <div class="label">Ollama</div>
            <div class="sub">Local daemon · <span class="settings-mono">127.0.0.1:11434</span></div>
          </div>
          <span class="settings-badge ${olOn ? "on" : "off"}">${olOn ? "Detected" : "Not found"}</span>
        </div>
        <div class="settings-mono">${olOn
          ? ("Model: " + esc(status.ollama_model || "?"))
          : "Install/run: ollama run llama3.2"}</div>
        ${olOn ? modelList(olModels) : ""}
      </div>
      <div class="settings-card">
        <div class="settings-row">
          <div>
            <div class="label">Active local route</div>
            <div class="sub">Auto-detected backend Cats AI will try first</div>
          </div>
          <span class="settings-badge ${active && active.name ? "on" : "off"}">${active && active.name ? esc(active.name) : "None"}</span>
        </div>
        <div class="settings-mono">${active && active.name
          ? esc((active.name || "") + (active.model ? " · " + active.model : "") + (active.base ? " · " + active.base : ""))
          : "No local LLM — Puter or API keys will be used."}</div>
        <div class="settings-actions">
          <button class="primary" id="settings-recheck">Re-check local LLMs</button>
          <button id="settings-copy-status">Copy status</button>
        </div>
      </div>
      <div class="sec-title">Cloud / keys</div>
      <div class="settings-card">
        <div class="settings-row">
          <div>
            <div class="label">Puter (UI)</div>
            <div class="sub">Free user-pays chat inside the window</div>
          </div>
          <span class="settings-badge on">Available</span>
        </div>
        <div class="settings-row">
          <div>
            <div class="label">Env API keys</div>
            <div class="sub">GROQ / OpenRouter / Gemini / Pollinations / OpenAI-compat</div>
          </div>
          <span class="settings-badge ${keyed.length ? "on" : "off"}">${keyed.length ? keyed.length + " ready" : "None"}</span>
        </div>
        <div class="settings-mono">${keyed.length ? esc(keyed.join(", ")) : "Optional: GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, POLLINATIONS_API_KEY"}</div>
      </div>
      <div class="sec-title">Tips</div>
      <div class="settings-card">
        <div class="sub">1. LM Studio: load a chat model, enable Local Server on port 1234.<br/>
        2. Click <strong style="color:var(--fg)">Re-check local LLMs</strong>.<br/>
        3. Badge turns green when CatIDE can see the server.</div>
      </div>
    </div>`;
  const re = $("#settings-recheck");
  if (re) re.onclick = () => refreshSettings(true);
  const copy = $("#settings-copy-status");
  if (copy) copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(status || {}, null, 2));
      toast("AI status copied");
    } catch (_) { toast("Copy failed"); }
  };
  refreshAiHint(status, null);
}
async function refreshSettings(toastOn) {
  const body = $("#explorer-body");
  if (body && state.view === "settings") {
    body.innerHTML = `<div class="settings-pane"><div class="settings-empty">Checking LM Studio / Ollama…</div></div>`;
  }
  try {
    const status = await api("/api/ai_status");
    state.aiStatus = status;
    if (state.view === "settings") renderSettings(status);
    refreshAiHint(status, null);
    if (toastOn) {
      if (status.lmstudio) toast("LM Studio detected · " + (status.lmstudio_model || "model ready"));
      else if (status.ollama) toast("Ollama detected · " + (status.ollama_model || "ready"));
      else toast("No local LLM found");
    }
    return status;
  } catch (e) {
    if (state.view === "settings" && body) {
      body.innerHTML = `<div class="settings-pane"><div class="settings-empty">Status check failed: ${String(e.message || e).replace(/</g,"&lt;")}</div>
        <div class="settings-actions"><button class="primary" id="settings-recheck">Retry</button></div></div>`;
      const re = $("#settings-recheck"); if (re) re.onclick = () => refreshSettings(true);
    }
    if (toastOn) toast("AI status failed");
    return null;
  }
}
function setView(view) {
  state.view = view;
  $$(".act-btn[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view===view));
  const titles = { explorer:"Explorer", search:"Search", git:"Source Control", run:"Run", ext:"Cats Market", diffs:"Diffs", settings:"Settings" };
  $("#sidebar-title").textContent = titles[view] || "Explorer";
  $("#search-panel").classList.toggle("visible", view==="search");
  $("#explorer-body").style.display = view==="search" ? "none" : "";
  const actions = document.querySelector(".sidebar-header .actions");
  if (actions) actions.style.display = view === "settings" ? "none" : "";
  if (view === "diffs") { showDiffs(); return; }
  if (view === "settings") { refreshSettings(false); return; }
  if (view === "git") $("#explorer-body").innerHTML = `<div style="padding:12px;color:var(--fg-muted);font-size:12px">Source Control<br/><br/>Changes appear after agent edits.<br/><button style="color:var(--accent-2);margin-top:8px" id="git-diffs">Open Diffs</button></div>`;
  else if (view === "run") $("#explorer-body").innerHTML = `<div style="padding:12px;color:var(--fg-muted);font-size:12px">Run and Debug<br/><br/><button style="color:var(--accent-2)">create launch.json</button></div>`;
  else if (view === "ext") $("#explorer-body").innerHTML = `<div style="padding:12px;color:var(--fg-muted);font-size:12px">Cats Marketplace<br/><br/>• Cats Core<br/>• Cats Agents<br/>• Design Mode<br/>• MCP Apps<br/>• Diff Review</div>`;
  else if (view === "explorer") renderTree();
  const gd = $("#git-diffs"); if (gd) gd.onclick = showDiffs;
}
function setPanel(name) {
  $$(".ptab").forEach(t => t.classList.toggle("active", t.dataset.panel===name));
  ["problems","output","debug","terminal"].forEach(id => { const el=$("#"+id); if (el) el.hidden = id!==name; });
  $("#term-row").hidden = name !== "terminal";
}
function showDiffs() {
  $("#editor-column").classList.add("show-diffs");
  $("#diffs-view").classList.add("visible");
  $("#editor-wrap").style.display = "none";
  const body = $("#diffs-body");
  if (!state.diffs.length) {
    body.innerHTML = `<div style="color:var(--fg-muted);padding:8px">No agent changes yet. Run a Cats Agent that edits files, then review here — stage, commit, and open a PR.</div>`;
    return;
  }
  body.innerHTML = state.diffs.map((d) => `<div class="diff-file"><div class="dh">${d.path}${state.staged ? " · staged" : ""}</div><div class="db"><span class="diff-add">+ agent update</span>\n${(d.summary||"").replace(/</g,"&lt;")}</div></div>`).join("");
}
function renderMarkdownLite(text) {
  let html = (text||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  html = html.replace(/```([\s\S]*?)```/g,(_,c)=>`<pre>${c}</pre>`);
  return html.replace(/`([^`]+)`/g,"<code>$1</code>");
}
function renderToolSteps(steps) {
  if (!steps||!steps.length) return "";
  return `<div class="tool-steps">${steps.map(s=>`<div class="tool-step"><div class="ts-name">${s.name}</div><div class="ts-args">${JSON.stringify(s.args||{}).slice(0,140).replace(/</g,"&lt;")}</div><div class="ts-result">${(s.result||"").slice(0,400).replace(/</g,"&lt;")}</div></div>`).join("")}</div>`;
}
function ensureScrollBottom(box) {
  let btn = $("#scroll-bottom");
  if (!btn) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "scroll-bottom";
    btn.id = "scroll-bottom";
    btn.textContent = "↓ Latest";
    btn.onclick = () => { box.scrollTop = box.scrollHeight; };
  }
  return btn;
}
function renderMessages() {
  const box = $("#ai-messages");
  if (!box) return;
  const agent = activeAgent();
  const msgs = (agent && Array.isArray(agent.messages)) ? agent.messages : [];
  if (!msgs.length) {
    box.innerHTML = `<div class="ai-empty agents-empty-hero"><h2>Build with Cats Agents</h2><p>Unified agent workspace — local, cloud, worktrees, and SSH — with IDE depth when you need files.</p><div class="starter">
      <button type="button" data-starter="Fix bugs in this repo and add tests."><strong>Fix &amp; test</strong>Agent explores and patches</button>
      <button type="button" data-starter="Explain the architecture of this project."><strong>Understand codebase</strong>Ask mode overview</button>
      <button type="button" data-starter="/best-of-n Implement a small improvement and compare approaches."><strong>Best of N</strong>Parallel agent comparison</button>
      <button type="button" data-starter="/worktree Create an isolated change for a safe experiment."><strong>Worktree</strong>Isolated agent checkout</button>
    </div></div>`;
    box.querySelectorAll("[data-starter]").forEach((b) => {
      b.onclick = () => {
        const text = b.getAttribute("data-starter") || "";
        if (text.startsWith("Explain")) state.aiMode = "ask";
        $$(".ai-modes button").forEach((x) => x.classList.toggle("active", x.dataset.mode === state.aiMode));
        const lab = $("#ai-mode-label"); if (lab) lab.textContent = modeLabel();
        sendChat(text);
      };
    });
    box.appendChild(ensureScrollBottom(box));
    return;
  }
  box.innerHTML = "";
  msgs.forEach((m) => {
    if (!m) return;
    const el = document.createElement("div");
    el.className = `msg ${m.role || "assistant"}${m.thinking?" thinking":""}`;
    el.innerHTML = `<div class="role">${m.role==="user"?"You":"Agent"}</div>${renderToolSteps(m.steps)}<div class="bubble">${renderMarkdownLite(m.content||"")}</div>`;
    box.appendChild(el);
  });
  box.appendChild(ensureScrollBottom(box));
  box.scrollTop = box.scrollHeight;
}
async function applyAgentFiles(files) {
  if (!files||!files.length) return;
  for (const path of files) {
    try {
      const data = await api("/api/read?path="+encodeURIComponent(path));
      state.contents[path] = data.content;
      if (!state.openTabs.includes(path)) state.openTabs.push(path);
      state.dirty[path] = false;
      state.diffs.unshift({ path, summary: "Agent updated this file." });
    } catch(_){}
  }
  await loadTree();
  if (files[0]) switchTab(files[0]);
  toast("Agent updated " + files.join(", "));
}
function normalizePuterText(resp) {
  if (resp == null) return "";
  if (typeof resp === "string") return resp;
  if (typeof resp === "number" || typeof resp === "boolean") return String(resp);
  if (typeof resp.text === "string") return resp.text;
  if (typeof resp.content === "string") return resp.content;
  if (resp.message) {
    if (typeof resp.message === "string") return resp.message;
    if (typeof resp.message.content === "string") return resp.message.content;
    if (Array.isArray(resp.message.content)) {
      return resp.message.content.map((p) => (typeof p === "string" ? p : (p && p.text) || "")).join("");
    }
  }
  if (Array.isArray(resp)) return resp.map(normalizePuterText).join("");
  try { return JSON.stringify(resp); } catch (_) { return String(resp); }
}
function parseClientToolCalls(content) {
  const calls = [];
  if (!content) return calls;
  const re = /```(?:tool|json)\s*([\s\S]*?)```/gi;
  let m;
  while ((m = re.exec(content))) {
    try {
      const obj = JSON.parse(m[1].trim());
      if (obj && (obj.name || obj.tool)) {
        calls.push({ name: obj.name || obj.tool, arguments: obj.arguments || obj.args || {} });
      }
    } catch (_) {}
  }
  return calls;
}
function stripClientToolMarkup(content) {
  return String(content || "")
    .replace(/```(?:tool|json)\s*[\s\S]*?```/gi, "")
    .trim();
}
async function catsPuterChat(messages) {
  if (typeof puter === "undefined" || !puter || !puter.ai || typeof puter.ai.chat !== "function") {
    throw new Error("Puter unavailable — check network / allow Puter sign-in");
  }
  const models = [
    "gpt-4o-mini",
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.1-8b-instruct",
    "google/gemini-2.0-flash",
  ];
  let lastErr = null;
  for (const model of models) {
    for (const testMode of [false, true]) {
      try {
        const resp = await puter.ai.chat(messages, { model, testMode });
        const content = normalizePuterText(resp);
        if (content && content.trim()) return content.trim();
      } catch (e) {
        lastErr = e;
      }
    }
  }
  throw lastErr || new Error("Puter returned empty response");
}
function refreshAiHint(status, provider) {
  const el = $("#ai-hint");
  if (!el) return;
  const bits = [];
  if (provider) bits.push("via " + provider);
  if (status && status.active_local && status.active_local.name) {
    bits.push(status.active_local.name + (status.active_local.model ? ":" + status.active_local.model : ""));
  } else {
    if (status && status.lmstudio) bits.push("LM Studio ready");
    if (status && status.ollama) bits.push("Ollama ready");
  }
  if (status && status.keyed_providers && status.keyed_providers.length) bits.push(status.keyed_providers.join("+"));
  bits.push("Puter free AI");
  el.textContent = "Cats AI · " + bits.join(" · ") + " · Enter send";
}
async function runAgentViaPuter(payload) {
  const boot = await api("/api/agent/bootstrap", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const messages = [{ role: "system", content: boot.system || "" }];
  (payload.history || []).slice(-10).forEach((h) => {
    if (h && (h.role === "user" || h.role === "assistant") && h.content) {
      messages.push({ role: h.role, content: h.content });
    }
  });
  messages.push({ role: "user", content: payload.message || "" });
  const steps = [];
  const filesChanged = [];
  const termChunks = [];
  const maxSteps = boot.max_steps || 5;
  let finalText = "";
  for (let i = 0; i < maxSteps; i++) {
    const content = await catsPuterChat(messages);
    const calls = parseClientToolCalls(content);
    if (!calls.length) {
      finalText = stripClientToolMarkup(content) || content;
      break;
    }
    messages.push({ role: "assistant", content });
    for (const call of calls) {
      const tr = await api("/api/tool", {
        method: "POST",
        body: JSON.stringify({
          name: call.name,
          arguments: call.arguments || {},
          mode: payload.mode || "agent",
        }),
      });
      const result = (tr && tr.result != null) ? String(tr.result) : "";
      steps.push({ name: call.name, args: call.arguments || {}, result: result.slice(0, 2000) });
      if (tr && tr.files_changed) filesChanged.push(...tr.files_changed);
      if (tr && tr.terminal_log) termChunks.push(tr.terminal_log);
      messages.push({
        role: "user",
        content: `Tool result [${call.name}]:\n${result.slice(0, 12000)}\n\nContinue. If done, summarize without more tools.`,
      });
    }
  }
  if (!finalText) {
    try {
      messages.push({ role: "user", content: "Stop using tools. Summarize what you accomplished." });
      finalText = stripClientToolMarkup(await catsPuterChat(messages));
    } catch (_) {
      finalText = steps.length ? `Completed ${steps.length} tool step(s).` : "No response from model.";
    }
  }
  return {
    content: finalText,
    steps,
    files_changed: [...new Set(filesChanged)],
    terminal_log: termChunks.join("\n"),
    provider: "puter",
  };
}
async function sendChat(overrideText, forceMode) {
  const input = $("#ai-input");
  const text = (overrideText != null ? overrideText : (input ? input.value : "")).trim();
  if (!text || state.aiBusy) return;
  if (overrideText == null && input) input.value = "";
  const agent = activeAgent();
  if (!agent) { toast("No agent available"); return; }
  if (!Array.isArray(agent.messages)) agent.messages = [];
  if ((agent.title || "").startsWith("New Agent") || (agent.title || "").startsWith("Agent ") || (agent.title || "").startsWith("Cats Agent")) {
    agent.title = text.slice(0, 28) + (text.length > 28 ? "…" : "");
  }
  agent.messages.push({ role:"user", content: text });
  agent.messages.push({ role:"assistant", content:"Working…", thinking:true, steps:[] });
  renderAgentsRail(); renderAiTabs(); renderMessages();
  state.aiBusy = true;
  const sendBtn = $("#ai-send"); if (sendBtn) sendBtn.disabled = true;
  const openFiles = {};
  state.openTabs.forEach((p) => {
    if (state.contents[p] != null) openFiles[p] = String(state.contents[p]).slice(0, 6000);
  });
  const hist = agent.messages
    .filter((m) => m && !m.thinking && m.content)
    .slice(0, -1)
    .slice(-12)
    .map(({ role, content }) => ({ role, content: content || "" }));
  let mode = forceMode || state.aiMode;
  if (text.startsWith("/worktree")) toast("Worktree: isolating changes in Local workspace");
  if (text.startsWith("/best-of-n")) toast("Best-of-N: running comparison across agent tabs");
  const payload = {
    message: text, history: hist, mode, active_path: state.activePath, open_files: openFiles, env: state.env,
  };
  try {
    let data = null;
    let routeNote = "";
    let status = null;
    try { status = await api("/api/ai_status"); } catch (_) {}
    const hasLocalFree = !!(status && (
      status.local || status.lmstudio || status.ollama ||
      (status.keyed_providers && status.keyed_providers.length)
    ));
    // Local Ollama / free env keys first; else Puter (avoids Pollinations 402 on coding)
    if (hasLocalFree) {
      try {
        data = await api("/api/agent", { method:"POST", body: JSON.stringify(payload) });
      } catch (e) {
        routeNote = String(e && e.message ? e.message : e);
      }
    }
    if (!data) {
      try {
        data = await runAgentViaPuter(payload);
        if (routeNote) toast("Fell back to Puter");
      } catch (puterErr) {
        if (!hasLocalFree) {
          try {
            data = await api("/api/agent", { method:"POST", body: JSON.stringify(payload) });
          } catch (be) {
            throw new Error(
              (puterErr && puterErr.message ? puterErr.message : puterErr) +
              " · backend: " + (be && be.message ? be.message : be)
            );
          }
        } else {
          throw puterErr;
        }
      }
    }
    if (agent.messages.length) agent.messages.pop();
    agent.messages.push({
      role:"assistant",
      content: ((data && data.content) || "(empty)") + (data && data.provider ? `\n\n_(via ${data.provider})_` : ""),
      steps: (data && data.steps) || [],
    });
    if (data && data.files_changed && data.files_changed.length) await applyAgentFiles(data.files_changed);
    if (data && data.terminal_log && $("#terminal")) {
      $("#terminal").innerHTML += String(data.terminal_log).replace(/</g,"&lt;") + "\n";
    }
    refreshAiHint(status, data && data.provider);
  } catch (e) {
    if (agent.messages.length) agent.messages.pop();
    agent.messages.push({ role:"assistant", content: "Agent error: " + (e && e.message ? e.message : e) });
  }
  state.aiBusy = false;
  if (sendBtn) sendBtn.disabled = false;
  renderMessages(); renderAgentsRail(); renderAiTabs();
}
async function runTerminal(cmd) {
  const term = $("#terminal");
  term.innerHTML += `$ ${cmd.replace(/</g,"&lt;")}\n`;
  try {
    const data = await api("/api/exec", { method:"POST", body: JSON.stringify({ cmd }) });
    term.innerHTML += (data.output||"").replace(/</g,"&lt;") + "\n";
  } catch (e) { term.innerHTML += e.message.replace(/</g,"&lt;") + "\n"; }
}
function openPalette() {
  $("#palette-overlay").classList.add("show");
  const cmds = [
    { label: "Cats Agents", k:"⌘⇧A", run: () => setLayout("agents") },
    { label: "Cats Editor", k:"⌘⇧E", run: () => setLayout("editor") },
    { label: "New Agent Tab", k:"", run: newAgent },
    { label: "Toggle Agent Panel", k:"⌘L", run: () => $("#ai-panel").classList.toggle("hidden") },
    { label: "Inline Edit", k:"⌘K", run: openInline },
    { label: "Design Mode", k:"⌘⇧D", run: toggleDesign },
    { label: "Open Browser", k:"", run: () => { state.browserOpen=true; $("#browser-view").classList.add("visible"); } },
    { label: "Review Diffs", k:"", run: showDiffs },
    { label: "Toggle Terminal", k:"⌘J", run: () => $("#panel").classList.toggle("collapsed") },
    { label: "Open Folder…", k:"⌘O", run: openFolder },
    { label: "New File", k:"⌘N", run: newUntitled },
    { label: "Save File", k:"⌘S", run: saveActive },
    { label: "Toggle Sidebar", k:"⌘B", run: () => $("#sidebar").classList.toggle("hidden") },
    { label: "Settings · LM Studio / Ollama", k:"⌘,", run: openSettings },
    { label: "Re-check local LLMs", k:"", run: () => { openSettings(); refreshSettings(true); } },
  ];
  const list = $("#palette-list");
  const draw = (q="") => {
    list.innerHTML = "";
    cmds.filter(c => c.label.toLowerCase().includes(q.toLowerCase())).forEach((c,i) => {
      const el = document.createElement("div");
      el.className = "palette-item" + (i===0?" active":"");
      el.innerHTML = `<span>${c.label}</span><span class="k">${c.k}</span>`;
      el.onclick = () => { $("#palette-overlay").classList.remove("show"); c.run(); };
      list.appendChild(el);
    });
  };
  draw();
  const inp = $("#palette-input"); inp.value=""; inp.focus();
  inp.oninput = () => draw(inp.value);
  inp.onkeydown = (e) => {
    if (e.key === "Escape") $("#palette-overlay").classList.remove("show");
    if (e.key === "Enter") { const a = list.querySelector(".palette-item"); if (a) a.click(); }
  };
}
function openInline() {
  $("#inline-overlay").classList.add("show");
  $("#inline-input").value = ""; $("#inline-input").focus();
}
function toggleDesign() {
  state.designMode = !state.designMode;
  state.browserOpen = true;
  $("#browser-view").classList.add("visible");
  $("#browser-stage").classList.toggle("design", state.designMode);
  $("#btn-design-mode").style.borderColor = state.designMode ? "var(--accent-2)" : "";
  toast(state.designMode ? "Design Mode on · Shift-drag to annotate" : "Design Mode off");
}

// events
$$(".act-btn[data-view]").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
$$(".ptab").forEach(t => t.addEventListener("click", () => setPanel(t.dataset.panel)));
$$(".ai-modes button").forEach(b => b.addEventListener("click", () => {
  state.aiMode = b.dataset.mode;
  $$(".ai-modes button").forEach(x => x.classList.toggle("active", x===b));
  $("#ai-mode-label").textContent = modeLabel();
}));
$$(".env-chips .chip").forEach(c => c.addEventListener("click", () => {
  state.env = c.dataset.env; $$(".env-chips .chip").forEach(x => x.classList.toggle("on", x===c));
  $("#ai-env-label").textContent = c.dataset.env[0].toUpperCase()+c.dataset.env.slice(1);
  const ag = activeAgent(); if (ag) ag.env = state.env; renderAgentsRail();
}));
$("#btn-editor-layout").onclick = () => setLayout("editor");
$("#btn-agents-layout").onclick = () => setLayout("agents");
$("#btn-ai-toggle").onclick = () => $("#ai-panel").classList.toggle("hidden");
if ($("#btn-new-agent")) $("#btn-new-agent").onclick = () => newAgent();
if ($("#btn-agents-settings")) $("#btn-agents-settings").onclick = () => openSettings();
if ($("#ai-clear")) $("#ai-clear").onclick = () => {
  const a = activeAgent();
  if (a) a.messages = [];
  renderMessages(); renderAgentsRail();
};
$("#ai-send").onclick = () => sendChat();
$("#ai-input").addEventListener("keydown", e => { if (e.key==="Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }});
$("#code").addEventListener("input", () => { if (!state.activePath) return; state.contents[state.activePath]=$("#code").value; state.dirty[state.activePath]=true; renderTabs(); updateStatus(); });
$("#code").addEventListener("scroll", () => { $("#gutter").scrollTop = $("#code").scrollTop; });
$("#code").addEventListener("keyup", updateStatus); $("#code").addEventListener("click", updateStatus);
if ($("#btn-refresh")) $("#btn-refresh").onclick = loadTree;
if ($("#btn-new-file")) $("#btn-new-file").onclick = () => { state.ctxPath=""; state.ctxType="dir"; newUntitled(); };
if ($("#btn-new-folder")) $("#btn-new-folder").onclick = () => { state.ctxPath=""; state.ctxType="dir"; newFolder(); };
if ($("#welcome-new")) $("#welcome-new").onclick = () => { state.ctxPath=""; state.ctxType="dir"; newUntitled(); };
if ($("#welcome-ai")) $("#welcome-ai").onclick = () => { $("#ai-panel").classList.remove("hidden"); $("#ai-input").focus(); };
$("#modal-cancel").onclick = hideModal;
$("#modal-ok").onclick = () => {
  const v = $("#modal-input").value;
  const r = modalResolve; modalResolve = null;
  $("#modal-overlay").classList.remove("show");
  if (r) r(v);
};
$("#modal-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#modal-ok").click(); }
  if (e.key === "Escape") hideModal();
});
$("#modal-overlay").addEventListener("click", (e) => { if (e.target.id === "modal-overlay") hideModal(); });
document.addEventListener("pointerdown", (e) => {
  if (e.target.closest && (e.target.closest("#ctx-menu") || e.target.closest(".menubar button"))) return;
  hideCtx();
});
document.addEventListener("contextmenu", (e) => {
  // Editor / tabs / blank areas — Cats menus
  const t = e.target;
  if (t.closest && t.closest("#ctx-menu")) return;
  if (t.closest && (t.closest("#explorer-body") || t.closest(".tree-item"))) return; // handled on nodes
  if (t.id === "code" || (t.closest && t.closest(".editor-area"))) {
    e.preventDefault();
    const sel = window.getSelection ? String(window.getSelection()) : "";
    showCtx(e.clientX, e.clientY, [
      { label: "Cut", run: async () => document.execCommand("cut") },
      { label: "Copy", run: async () => document.execCommand("copy") },
      { label: "Paste", run: async () => document.execCommand("paste") },
      "-",
      { label: "Command Palette", run: openPalette },
      { label: "Inline Edit (⌘K)", run: openInline },
      { label: "Ask Agent", run: async () => {
        $("#ai-panel").classList.remove("hidden");
        const snippet = ($("#code").value || "").slice(Math.max(0, ($("#code").selectionStart||0)-200), ($("#code").selectionEnd||0)+200);
        $("#ai-input").value = (sel ? `About selection:\n${sel}\n\n` : "") + (state.activePath ? `File: ${state.activePath}\n` : "") + "Help with this code.";
        $("#ai-input").focus();
      }},
      { label: "New File…", run: newUntitled },
      { label: "Save", run: saveActive },
    ]);
    return;
  }
  if (t.closest && t.closest("#tabs")) {
    e.preventDefault();
    showCtx(e.clientX, e.clientY, [
      { label: "New File…", run: newUntitled },
      { label: "Close Editor", run: async () => { if (state.activePath) closeTab(state.activePath); } },
      { label: "Close Others", run: async () => {
        const keep = state.activePath; state.openTabs = keep ? [keep] : []; renderTabs();
      }},
    ]);
  }
});
$("#welcome-agents").onclick = () => setLayout("agents");
if ($("#welcome-folder")) $("#welcome-folder").onclick = openFolder;
$("#welcome-open").onclick = () => $("#file-picker").click();
if ($("#btn-open-folder")) $("#btn-open-folder").onclick = openFolder;
if ($("#workspace-pill")) $("#workspace-pill").onclick = openFolder;
$("#file-picker").onchange = async (e) => { for (const f of e.target.files) { state.contents[f.name]=await f.text(); if (!state.openTabs.includes(f.name)) state.openTabs.push(f.name); state.dirty[f.name]=true; switchTab(f.name); } };
$("#term-input").addEventListener("keydown", e => { if (e.key==="Enter") { const cmd=e.target.value.trim(); e.target.value=""; if (cmd) runTerminal(cmd); }});
$("#btn-palette").onclick = openPalette; $("#btn-inline").onclick = openInline;
$("#btn-browser").onclick = () => { state.browserOpen=!state.browserOpen; $("#browser-view").classList.toggle("visible", state.browserOpen); };
$("#btn-browser-close").onclick = () => { state.browserOpen=false; $("#browser-view").classList.remove("visible"); };
$("#btn-design-mode").onclick = toggleDesign;
$("#btn-close-diffs").onclick = () => {
  $("#diffs-view").classList.remove("visible");
  $("#editor-column").classList.remove("show-diffs");
  $("#editor-wrap").style.display = "";
};
if ($("#btn-stage-all")) $("#btn-stage-all").onclick = () => {
  if (!state.diffs.length) { toast("Nothing to stage"); return; }
  state.staged = true; showDiffs(); toast("Staged " + state.diffs.length + " change(s)");
};
if ($("#btn-commit")) $("#btn-commit").onclick = async () => {
  if (!state.diffs.length) { toast("No changes"); return; }
  const msg = ($("#commit-msg").value || "Cats agent changes").trim();
  try {
    await api("/api/exec", { method: "POST", body: JSON.stringify({ cmd: `git add -A && git commit -m ${JSON.stringify(msg)} || echo 'commit skipped (no git)'` }) });
    toast("Commit: " + msg); state.diffs = []; state.staged = false; showDiffs();
  } catch (e) { toast(e.message); }
};
if ($("#btn-pr")) $("#btn-pr").onclick = async () => {
  toast("Create PR · opening git status");
  setPanel("terminal"); $("#panel").classList.remove("collapsed");
  await runTerminal("git status && git branch --show-current");
};
if ($("#btn-tile-single")) $("#btn-tile-single").onclick = () => setTileMode("single");
if ($("#btn-tile-split")) $("#btn-tile-split").onclick = () => setTileMode("split");
if ($("#btn-tile-grid")) $("#btn-tile-grid").onclick = () => setTileMode("grid");
if ($("#btn-handoff-cloud")) $("#btn-handoff-cloud").onclick = () => handoffAgent("cloud");
if ($("#btn-handoff-local")) $("#btn-handoff-local").onclick = () => handoffAgent("local");
document.querySelectorAll("[data-starter]").forEach((b) => {
  b.onclick = () => {
    const text = b.getAttribute("data-starter");
    if (text.startsWith("Explain")) { state.aiMode = "ask"; $$(".ai-modes button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "ask")); $("#ai-mode-label").textContent = modeLabel(); }
    sendChat(text);
  };
});
$("#btn-settings").onclick = (e) => { e.preventDefault(); e.stopPropagation(); openSettings(); };
// Activity bar settings also has data-view — prefer openSettings so Agents layout switches to Editor
$$('.act-btn[data-view="settings"]').forEach((b) => {
  b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openSettings(); }, true);
});
$("#inline-send").onclick = async () => {
  const t = $("#inline-input").value.trim(); if (!t) return;
  $("#inline-overlay").classList.remove("show");
  await sendChat("Inline edit request: " + t + (state.activePath ? `\nFile: ${state.activePath}` : ""), "edit");
};
$("#palette-overlay").addEventListener("click", e => { if (e.target.id==="palette-overlay") e.target.classList.remove("show"); });
$("#inline-overlay").addEventListener("click", e => { if (e.target.id==="inline-overlay") e.target.classList.remove("show"); });
$("#ai-messages").addEventListener("scroll", () => {
  const box = $("#ai-messages");
  $("#scroll-bottom").classList.toggle("show", box.scrollHeight - box.scrollTop - box.clientHeight > 80);
});
$("#scroll-bottom").onclick = () => { const box=$("#ai-messages"); box.scrollTop = box.scrollHeight; };
const stage = $("#browser-stage");
let drag = null;
stage.addEventListener("mousedown", e => {
  if (!state.designMode || !e.shiftKey) return;
  const r = stage.getBoundingClientRect();
  drag = { x: e.clientX - r.left, y: e.clientY - r.top, el: document.createElement("div") };
  drag.el.className = "anno"; drag.el.style.left = drag.x+"px"; drag.el.style.top = drag.y+"px";
  stage.appendChild(drag.el);
});
window.addEventListener("mousemove", e => {
  if (!drag) return; const r = stage.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  drag.el.style.width = Math.abs(x-drag.x)+"px"; drag.el.style.height = Math.abs(y-drag.y)+"px";
  drag.el.style.left = Math.min(x,drag.x)+"px"; drag.el.style.top = Math.min(y,drag.y)+"px";
});
window.addEventListener("mouseup", () => { drag = null; });
document.addEventListener("keydown", e => {
  const meta = e.metaKey || e.ctrlKey;
  if (meta && e.key.toLowerCase()==="s") { e.preventDefault(); saveActive(); }
  if (meta && e.key.toLowerCase()==="o") { e.preventDefault(); openFolder(); }
  if (meta && e.key.toLowerCase()==="n") { e.preventDefault(); newUntitled(); }
  if (meta && e.key.toLowerCase()==="b") { e.preventDefault(); $("#sidebar").classList.toggle("hidden"); }
  if (meta && e.key.toLowerCase()==="j") { e.preventDefault(); $("#panel").classList.toggle("collapsed"); }
  if (meta && e.key.toLowerCase()==="l") { e.preventDefault(); $("#ai-panel").classList.toggle("hidden"); }
  if (meta && e.key.toLowerCase()==="k") { e.preventDefault(); openInline(); }
  if (meta && e.key.toLowerCase()==="p") { e.preventDefault(); openPalette(); }
  if (meta && e.key === ",") { e.preventDefault(); openSettings(); }
  if (meta && e.shiftKey && e.key.toLowerCase()==="a") { e.preventDefault(); setLayout("agents"); }
  if (meta && e.shiftKey && e.key.toLowerCase()==="e") { e.preventDefault(); setLayout("editor"); }
  if (meta && e.shiftKey && e.key.toLowerCase()==="d") { e.preventDefault(); toggleDesign(); }
  if (meta && e.key === "`") { e.preventDefault(); $("#panel").classList.remove("collapsed"); setPanel("terminal"); }
  if (e.key === "Escape") {
    $("#palette-overlay").classList.remove("show");
    $("#inline-overlay").classList.remove("show");
    hideModal();
    hideCtx();
  }
});
$$(".menubar button").forEach(b => b.addEventListener("click", (ev) => {
  const m = b.dataset.menu;
  if (m==="agent") setLayout("agents");
  if (m==="view") openPalette();
  if (m==="terminal") { $("#panel").classList.remove("collapsed"); setPanel("terminal"); }
  if (m==="help") {
    ev.preventDefault(); ev.stopPropagation();
    const r = b.getBoundingClientRect();
    showCtx(r.left, r.bottom + 4, [
      { label: "Settings…", run: openSettings },
      { label: "Re-check LM Studio / Ollama", run: () => { openSettings(); refreshSettings(true); } },
      "-",
      { label: "About CatIDE0.1", run: () => toast("CatIDE0.1 · Cats · Agents · Design Mode · Diffs") },
    ]);
  }
  if (m==="file") {
    ev.preventDefault(); ev.stopPropagation();
    const r = b.getBoundingClientRect();
    showCtx(r.left, r.bottom + 4, [
      { label: "Open Folder…", run: openFolder },
      { label: "New File…", run: newUntitled },
      { label: "New Folder…", run: newFolder },
      { label: "Open File…", run: async () => $("#file-picker").click() },
      "-",
      { label: "Save", run: saveActive },
      { label: "Save As…", run: async () => {
        if (!state.activePath) return;
        const name = await askModal({ title:"Save As", hint:"File name", value: state.activePath.startsWith("Untitled-") ? "untitled.py" : state.activePath, okLabel:"Save" });
        if (!name) return;
        try {
          const content = $("#code").value;
          await createFileOnDisk(name.trim(), content);
        } catch (e) { toast(e.message); }
      }},
    ]);
  }
  if (m==="edit") {
    ev.preventDefault(); ev.stopPropagation();
    const r = b.getBoundingClientRect();
    showCtx(r.left, r.bottom + 4, [
      { label: "Undo", run: async () => document.execCommand("undo") },
      { label: "Redo", run: async () => document.execCommand("redo") },
      "-",
      { label: "Cut", run: async () => document.execCommand("cut") },
      { label: "Copy", run: async () => document.execCommand("copy") },
      { label: "Paste", run: async () => document.execCommand("paste") },
    ]);
  }
}));
function makeResize(el, onMove) {
  el.addEventListener("mousedown", e => {
    e.preventDefault();
    const move = ev => onMove(ev); const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  });
}
makeResize($("#resize-sidebar"), e => {
  const left = $("#activity").getBoundingClientRect().right;
  $("#sidebar").style.width = Math.min(420, Math.max(180, e.clientX - left)) + "px";
});
makeResize($("#resize-ai"), e => { $("#ai-panel").style.width = Math.min(640, Math.max(300, window.innerWidth - e.clientX)) + "px"; });
makeResize($("#resize-panel"), e => { $("#panel").style.height = Math.min(400, Math.max(100, window.innerHeight - e.clientY - 24)) + "px"; });
$("#search-input").addEventListener("input", async (e) => {
  const q = e.target.value.trim();
  if (!q) { $("#search-results").textContent = "Type to search"; return; }
  try {
    const data = await api("/api/search?q="+encodeURIComponent(q));
    if (!data.results.length) { $("#search-results").textContent = "No results"; return; }
    $("#search-results").innerHTML = data.results.slice(0,50).map(r =>
      `<div style="padding:4px 0;cursor:pointer" class="search-hit" data-path="${r.path.replace(/"/g,"&quot;")}"><strong>${r.path}</strong>:${r.line}<br/><span style="color:var(--fg-muted)">${r.preview.replace(/</g,"&lt;")}</span></div>`
    ).join("");
    $$(".search-hit").forEach(el => el.onclick = () => openFile(el.dataset.path));
  } catch (err) { $("#search-results").textContent = err.message; }
});
setPanel("problems");
setLayout("agents");
renderAgentsRail(); renderAiTabs(); renderMessages();
$("#ai-mode-label").textContent = modeLabel();
loadTree().then(() => {
  if ($("#workspace-pill") && state.files?.name) $("#workspace-pill").textContent = state.files.name;
});
api("/api/ai_status").then((s) => refreshAiHint(s, null)).catch(() => {});
</script>
</body>
</html>
"""


class CatIDEHandler(BaseHTTPRequestHandler):
    server_version = "CatIDE/0.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[CatIDE] " + (fmt % args) + "\n")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _safe_path(self, rel: str) -> Path:
        rel = unquote(rel).lstrip("/").replace("\\", "/")
        if rel.startswith("Untitled-"):
            raise ValueError("untitled")
        target = (WORKSPACE / rel).resolve()
        if not str(target).startswith(str(WORKSPACE.resolve())):
            raise PermissionError("Path outside workspace")
        return target

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._text(200, HTML, "text/html; charset=utf-8")
            return

        if path == "/api/workspace":
            self._json(200, {"path": str(WORKSPACE), "name": WORKSPACE.name})
            return

        if path == "/api/ai_status":
            self._json(200, ai_status())
            return

        if path == "/api/tree":
            self._json(200, build_tree(WORKSPACE))
            return

        if path == "/api/read":
            rel = (qs.get("path") or [""])[0]
            try:
                fp = self._safe_path(rel)
                if not fp.is_file():
                    self._json(404, {"error": "Not found"})
                    return
                content = fp.read_text(encoding="utf-8", errors="replace")
                self._json(200, {"path": rel, "content": content})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/search":
            q = (qs.get("q") or [""])[0].lower()
            results = []
            if q:
                for fp in WORKSPACE.rglob("*"):
                    if not fp.is_file():
                        continue
                    if any(p.startswith(".") for p in fp.parts):
                        continue
                    if fp.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".pdf"}:
                        continue
                    try:
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if q in line.lower():
                            rel = str(fp.relative_to(WORKSPACE))
                            results.append({"path": rel, "line": i, "preview": line.strip()[:120]})
                            if len(results) >= 100:
                                break
                    if len(results) >= 100:
                        break
            self._json(200, {"results": results})
            return

        # static fallback
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/api/open_folder":
            folder = body.get("path", "")
            try:
                ws = set_workspace(folder)
                self._json(200, {"ok": True, "path": str(ws), "name": ws.name})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/write":
            rel = body.get("path", "")
            content = body.get("content", "")
            try:
                fp = self._safe_path(rel)
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding="utf-8")
                self._json(200, {"ok": True, "path": rel})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/mkdir":
            rel = body.get("path", "")
            try:
                fp = self._safe_path(rel)
                fp.mkdir(parents=True, exist_ok=True)
                self._json(200, {"ok": True, "path": rel})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/delete":
            rel = body.get("path", "")
            try:
                fp = self._safe_path(rel)
                if fp == WORKSPACE.resolve():
                    raise PermissionError("Cannot delete workspace root")
                if fp.is_dir():
                    shutil.rmtree(fp)
                elif fp.is_file():
                    fp.unlink()
                else:
                    raise FileNotFoundError(rel)
                self._json(200, {"ok": True, "path": rel})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/rename":
            src = body.get("path", "")
            dest = body.get("dest", "")
            try:
                sp = self._safe_path(src)
                dp = self._safe_path(dest)
                if dp.exists():
                    raise FileExistsError(f"Already exists: {dest}")
                dp.parent.mkdir(parents=True, exist_ok=True)
                sp.rename(dp)
                self._json(200, {"ok": True, "path": dest})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/ai_status":
            self._json(200, ai_status())
            return

        if path == "/api/agent/bootstrap":
            mode = (body.get("mode") or "agent").lower()
            if mode not in {"agent", "ask", "edit", "plan", "manual"}:
                mode = "agent"
            system = agent_system_prompt(mode, body.get("active_path"), body.get("open_files") or {})
            self._json(200, {
                "system": system,
                "mode": mode,
                "max_steps": AGENT_MAX_STEPS,
                "ai": ai_status(),
            })
            return

        if path == "/api/tool":
            mode = (body.get("mode") or "agent").lower()
            name = body.get("name") or ""
            args = body.get("arguments") or body.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            try:
                result, changed, term = execute_tool(name, args, mode)
                self._json(200, {
                    "result": result,
                    "files_changed": changed or [],
                    "terminal_log": term or "",
                })
            except Exception as e:
                self._json(400, {"error": str(e), "result": f"Tool error: {e}", "files_changed": []})
            return

        if path == "/api/chat" or path == "/api/agent":
            try:
                result = run_cursor_agent(body)
                result["provider"] = _LAST_AI_PROVIDER
                self._json(200, result)
            except Exception as e:
                self._json(502, {
                    "error": str(e),
                    "content": f"Agent failed: {e}",
                    "steps": [],
                    "files_changed": [],
                    "ai": ai_status(),
                })
            return

        if path == "/api/exec":
            cmd = (body.get("cmd") or "").strip()
            if not cmd:
                self._json(400, {"error": "empty command"})
                return
            # Safety: allow only mild local commands
            blocked = ["rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot"]
            if any(b in cmd for b in blocked):
                self._json(403, {"output": "Command blocked for safety."})
                return
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(WORKSPACE),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                if not out:
                    out = f"(exit {proc.returncode})"
                self._json(200, {"output": out, "code": proc.returncode})
            except subprocess.TimeoutExpired:
                self._json(200, {"output": "Command timed out (30s).", "code": -1})
            except Exception as e:
                self._json(500, {"output": str(e)})
            return

        self._json(404, {"error": "Not found"})


def build_tree(root: Path, depth: int = 0, max_depth: int = 4) -> dict:
    node = {"name": root.name or str(root), "path": ".", "type": "dir", "children": []}
    if depth == 0:
        node["name"] = root.name
        node["path"] = "."
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return node
    for p in entries:
        if p.name.startswith(".") or p.name in {"__pycache__", "node_modules", ".git"}:
            continue
        rel = str(p.relative_to(WORKSPACE))
        if p.is_dir():
            child = {"name": p.name, "path": rel, "type": "dir", "children": []}
            if depth < max_depth:
                child = build_tree(p, depth + 1, max_depth)
                child["path"] = rel
                child["name"] = p.name
            node["children"].append(child)
        else:
            node["children"].append({"name": p.name, "path": rel, "type": "file"})
    return node


def safe_workspace_path(rel: str) -> Path:
    rel = unquote(str(rel or "")).lstrip("/").replace("\\", "/")
    if not rel or rel in (".", "./"):
        return WORKSPACE.resolve()
    target = (WORKSPACE / rel).resolve()
    root = WORKSPACE.resolve()
    if not str(target).startswith(str(root)):
        raise PermissionError("Path outside workspace")
    return target


def tool_defs_for_mode(mode: str) -> list:
    names = {
        "agent": {"read_file", "write_file", "str_replace", "list_dir", "search_codebase", "run_terminal"},
        "ask": {"read_file", "list_dir", "search_codebase"},
        "edit": {"read_file", "write_file", "str_replace", "list_dir", "search_codebase"},
        "plan": {"read_file", "list_dir", "search_codebase"},
        "manual": {"read_file", "write_file", "str_replace", "list_dir", "search_codebase"},
    }.get(mode, None)
    if names is None:
        names = {"read_file", "write_file", "str_replace", "list_dir", "search_codebase", "run_terminal"}
    return [t for t in AGENT_TOOL_DEFS if t["function"]["name"] in names]


def execute_tool(name: str, args: dict, mode: str) -> tuple[str, list[str], str]:
    """Returns (result_text, files_changed, terminal_log_chunk)."""
    allowed = {t["function"]["name"] for t in tool_defs_for_mode(mode)}
    if name not in allowed:
        return f"Tool '{name}' is not available in {mode} mode.", [], ""

    args = args or {}
    changed: list[str] = []
    term_log = ""

    try:
        if name == "read_file":
            fp = safe_workspace_path(args.get("path", ""))
            if not fp.is_file():
                return f"File not found: {args.get('path')}", [], ""
            text = fp.read_text(encoding="utf-8", errors="replace")
            if len(text) > 30000:
                text = text[:30000] + "\n… [truncated]"
            return text, [], ""

        if name == "write_file":
            rel = args.get("path", "")
            fp = safe_workspace_path(rel)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(args.get("content", ""), encoding="utf-8")
            changed.append(str(Path(rel)))
            return f"Wrote {rel} ({len(args.get('content', ''))} chars)", changed, ""

        if name == "str_replace":
            rel = args.get("path", "")
            fp = safe_workspace_path(rel)
            if not fp.is_file():
                return f"File not found: {rel}", [], ""
            text = fp.read_text(encoding="utf-8", errors="replace")
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            if old not in text:
                return "old_string not found in file (must match exactly).", [], ""
            if text.count(old) > 1:
                return "old_string found multiple times — make it unique.", [], ""
            fp.write_text(text.replace(old, new, 1), encoding="utf-8")
            changed.append(str(Path(rel)))
            return f"Updated {rel}", changed, ""

        if name == "list_dir":
            rel = args.get("path") or "."
            fp = safe_workspace_path(rel)
            if not fp.is_dir():
                return f"Not a directory: {rel}", [], ""
            lines = []
            for p in sorted(fp.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith(".") or p.name in {"__pycache__", "node_modules"}:
                    continue
                kind = "dir" if p.is_dir() else "file"
                lines.append(f"{kind}\t{p.name}")
            return "\n".join(lines) or "(empty)", [], ""

        if name == "search_codebase":
            q = (args.get("query") or "").lower()
            if not q:
                return "Empty query", [], ""
            hits = []
            for fp in WORKSPACE.rglob("*"):
                if not fp.is_file():
                    continue
                if any(part.startswith(".") for part in fp.relative_to(WORKSPACE).parts):
                    continue
                if fp.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".pdf"}:
                    continue
                try:
                    for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if q in line.lower():
                            hits.append(f"{fp.relative_to(WORKSPACE)}:{i}: {line.strip()[:120]}")
                            if len(hits) >= 40:
                                break
                except OSError:
                    continue
                if len(hits) >= 40:
                    break
            return "\n".join(hits) if hits else "No matches", [], ""

        if name == "run_terminal":
            cmd = (args.get("command") or "").strip()
            if not cmd:
                return "Empty command", [], ""
            blocked = ["rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot"]
            if any(b in cmd for b in blocked):
                return "Command blocked for safety.", [], ""
            proc = subprocess.run(
                cmd, shell=True, cwd=str(WORKSPACE),
                capture_output=True, text=True, timeout=45,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if not out:
                out = f"(exit {proc.returncode})"
            if len(out) > 12000:
                out = out[:12000] + "\n… [truncated]"
            term_log = f"$ {cmd}\n{out}"
            return out, [], term_log

        return f"Unknown tool: {name}", [], ""
    except Exception as e:
        return f"Tool error: {e}", [], ""


def parse_text_tool_calls(content: str) -> list[dict]:
    """Fallback parser when the model emits JSON tool blocks instead of native tool_calls."""
    calls = []
    if not content:
        return calls

    for m in re.finditer(r"```(?:tool|json)\s*([\s\S]*?)```", content, re.I):
        block = m.group(1).strip()
        try:
            obj = json.loads(block)
            if isinstance(obj, dict) and (obj.get("name") or obj.get("tool")):
                calls.append({
                    "id": f"text_{len(calls)}",
                    "name": obj.get("name") or obj.get("tool"),
                    "arguments": obj.get("arguments") or obj.get("args") or {},
                })
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and (item.get("name") or item.get("tool")):
                        calls.append({
                            "id": f"text_{len(calls)}",
                            "name": item.get("name") or item.get("tool"),
                            "arguments": item.get("arguments") or item.get("args") or {},
                        })
        except json.JSONDecodeError:
            continue

    for m in re.finditer(
        r"<tool_call>\s*<name>\s*(\w+)\s*</name>\s*<arguments>\s*([\s\S]*?)\s*</arguments>\s*</tool_call>",
        content,
        re.I,
    ):
        try:
            args = json.loads(m.group(2).strip())
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": f"xml_{len(calls)}", "name": m.group(1), "arguments": args})

    # TOOL_CALL name={...}
    for m in re.finditer(r"TOOL_CALL\s+(\w+)\s+(\{[\s\S]*?\})", content):
        try:
            args = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue
        calls.append({"id": f"line_{len(calls)}", "name": m.group(1), "arguments": args})

    return calls


def strip_tool_markup(content: str) -> str:
    if not content:
        return ""
    content = re.sub(r"```(?:tool|json)\s*[\s\S]*?```", "", content, flags=re.I)
    content = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", content, flags=re.I)
    content = re.sub(r"TOOL_CALL\s+\w+\s+\{[\s\S]*?\}", "", content)
    return content.strip()


def agent_system_prompt(mode: str, active_path: str | None, open_files: dict) -> str:
    tree = build_tree(WORKSPACE, max_depth=2)
    def flat(nodes, prefix=""):
        lines = []
        for n in (nodes or [])[:40]:
            lines.append(f"{prefix}{n['name']}{'/' if n.get('type')=='dir' else ''}")
            if n.get("children"):
                lines.extend(flat(n["children"], prefix + "  "))
        return lines
    listing = "\n".join(flat(tree.get("children") or []))

    mode_rules = {
        "agent": (
            "MODE: Agent (Cats Agents). Autonomous coding agent. "
            "Use tools to explore, edit files, and run commands until done. "
            "Prefer str_replace for small edits; write_file for new files. Read before edit."
        ),
        "ask": (
            "MODE: Ask (read-only). Answer questions using read/search/list tools only. "
            "Do not modify files or run shell commands."
        ),
        "edit": (
            "MODE: Edit. Focus on implementing code changes with write_file/str_replace. "
            "Read files as needed. Do not run arbitrary shell unless necessary."
        ),
        "plan": (
            "MODE: Plan. First produce a clear numbered implementation plan. "
            "You may read/search the codebase. Do not modify files yet unless asked to execute the plan."
        ),
        "manual": (
            "MODE: Manual. Make careful, guided edits. Explain each change briefly. "
            "Use write_file/str_replace; ask before large refactors."
        ),
    }.get(mode, "MODE: Agent.")

    open_ctx = ""
    if active_path:
        open_ctx += f"\nActive file: {active_path}\n"
    if open_files:
        for p, c in list(open_files.items())[:4]:
            open_ctx += f"\n--- {p} ---\n{c[:4000]}\n"

    return f"""You are Cat, the coding agent inside CatIDE0.1 (Cats).
The IDE shell is VS Code–like; you behave as a Cats Agent.

{mode_rules}

Workspace root: {WORKSPACE.name}
Workspace files:
{listing}
{open_ctx}

When you need tools, either use native function calling OR emit a fenced block:
```tool
{{"name": "read_file", "arguments": {{"path": "example.py"}}}}
```
Available tools depend on mode. After tools run you will see results — continue until finished.
Be concise. When done, give a short summary of what you did (no more tool calls)."""


def _http_error_detail(err: Exception) -> str:
    if isinstance(err, urllib.error.HTTPError):
        try:
            body = err.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        code = err.code
        if code == 403:
            return "403 Forbidden (blocked bot UA or Cloudflare) — retrying with browser headers"
        if code == 402:
            return "402 Payment Required — anonymous model/tools unavailable, trying fallback"
        if code == 429:
            return "429 rate limited — waiting and retrying"
        return f"HTTP {code}: {body[:180]}"
    return str(err)


def _parse_chat_response(data) -> dict | None:
    if isinstance(data, dict):
        choices = data.get("choices") or []
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get("message") or {}
            if not isinstance(msg, dict):
                # some providers put content directly on the choice
                content = first.get("content") or data.get("content") or ""
                return {"role": "assistant", "content": str(content), "tool_calls": []}
            return {
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or [],
            }
        if data.get("content"):
            return {"role": "assistant", "content": str(data["content"]), "tool_calls": []}
    if isinstance(data, str) and data.strip():
        return {"role": "assistant", "content": data.strip(), "tool_calls": []}
    return None


def _flatten_messages_for_get(messages: list) -> str:
    """Build a compact prompt for the free GET text API (no payment)."""
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            # Keep system short — URL length limits
            parts.append("System: " + content[:700])
        elif role == "assistant":
            parts.append("Assistant: " + content[:500])
        elif role == "tool":
            parts.append("Tool result: " + content[:700])
        else:
            parts.append("User: " + content[:900])
    # Prefer the tail of the conversation (most relevant)
    blob = "\n\n".join(parts[-6:])
    if len(blob) > 1400:
        blob = blob[-1400:]
    blob += (
        "\n\nAssistant: Respond helpfully. "
        "If you need a tool, emit a fenced ```tool JSON block with name and arguments."
    )
    return blob


def _sanitize_messages(messages: list, tools: list | None = None) -> list[dict]:
    clean_messages = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if content is None:
            content = ""
        item = {"role": role, "content": str(content)}
        if role == "assistant" and m.get("tool_calls") and AI_NATIVE_TOOLS:
            item["tool_calls"] = m["tool_calls"]
        if role == "tool" and AI_NATIVE_TOOLS:
            item["tool_call_id"] = m.get("tool_call_id") or "call_0"
        if role == "tool" and not AI_NATIVE_TOOLS:
            item = {"role": "user", "content": f"Tool result:\n{content}"}
        clean_messages.append(item)
    return clean_messages


def _llm_via_get(prompt: str) -> dict | None:
    """Anonymous Pollinations GET. Only tiny/non-coding prompts are free anymore."""
    prompt = (prompt or "Hello").strip() or "Hello"
    url = AI_GET_BASE + urllib.request.quote(prompt[:1200])
    url += ("&" if "?" in url else "?") + f"model=openai-fast&seed={int(time.time()) % 100000}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain",
            "User-Agent": AI_HTTP_HEADERS["User-Agent"],
            "Referer": AI_HTTP_HEADERS["Referer"],
            "Origin": AI_HTTP_HEADERS.get("Origin", "https://pollinations.ai/"),
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    if not text:
        return None
    low = text.lower()
    if "payment required" in low or '"status":402' in text.replace(" ", ""):
        return None
    if "get unlimited access at https://enter.pollinations.ai" in low and len(text) < 220:
        return None
    if text.lstrip().startswith("{") and "error" in low and "402" in text:
        return None
    return {"role": "assistant", "content": text, "tool_calls": []}


def _http_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 90):
    h = dict(AI_HTTP_HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=h,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _http_get_json(url: str, timeout: float = 1.2):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": AI_HTTP_HEADERS["User-Agent"],
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _base_no_v1(url: str) -> str:
    u = (url or "").rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u.rstrip("/")


def _openai_model_ids(data) -> list[str]:
    ids: list[str] = []
    if isinstance(data, dict):
        rows = data.get("data") or data.get("models") or []
        if isinstance(rows, list):
            for m in rows:
                if isinstance(m, dict):
                    mid = (m.get("id") or m.get("name") or m.get("model") or "").strip()
                    if mid:
                        ids.append(mid)
                elif isinstance(m, str) and m.strip():
                    ids.append(m.strip())
    return ids


def _ollama_models(host: str | None = None) -> list[str]:
    base = (host or OLLAMA_HOST).rstrip("/")
    try:
        data = _http_get_json(f"{base}/api/tags", timeout=1.5)
        names = []
        for m in data.get("models") or []:
            name = (m.get("name") or m.get("model") or "").strip()
            if name:
                names.append(name)
        return names
    except Exception:
        return []


def _score_local_model(name: str) -> tuple[int, str]:
    """Higher score = better default for Cats coding agent."""
    n = (name or "").lower()
    score = 0
    for token, pts in (
        ("coder", 50),
        ("code", 40),
        ("codestral", 55),
        ("deepseek-coder", 60),
        ("qwen2.5-coder", 58),
        ("qwen3-coder", 58),
        ("starcoder", 45),
        ("devstral", 50),
        ("instruct", 15),
        ("chat", 10),
        ("llama", 8),
        ("mistral", 8),
        ("phi", 6),
        ("gemma", 6),
    ):
        if token in n:
            score += pts
    # Prefer mid-size over tiny/huge when name encodes size
    if re.search(r"(^|[^0-9])(7b|8b|14b|15b|32b)([^0-9]|$)", n):
        score += 12
    if re.search(r"(^|[^0-9])(0\.5b|1b|1\.5b|3b)([^0-9]|$)", n):
        score -= 5
    if "embed" in n or "embedding" in n or "whisper" in n or "tts" in n:
        score -= 100
    return score, n


def _pick_best_model(models: list[str], preferred: str = "") -> str:
    if preferred and preferred in models:
        return preferred
    if preferred and not models:
        return preferred
    ranked = sorted((m for m in models if m), key=_score_local_model, reverse=True)
    if ranked:
        return ranked[0]
    return preferred or (models[0] if models else "")


def _probe_openai_compat(name: str, host: str, preferred_model: str = "") -> dict | None:
    """Detect an OpenAI-compatible local server (LM Studio, llama.cpp, etc.)."""
    root = _base_no_v1(host)
    if not root.startswith("http"):
        root = "http://" + root
    candidates = [f"{root}/v1/models", f"{root}/models"]
    models: list[str] = []
    v1_base = f"{root}/v1"
    for url in candidates:
        try:
            data = _http_get_json(url, timeout=1.0)
            models = _openai_model_ids(data)
            if "/v1/" in url or url.endswith("/v1/models"):
                v1_base = url.rsplit("/models", 1)[0]
            elif models:
                # some servers expose /models at root but chat under /v1
                v1_base = f"{root}/v1"
            if models or data is not None:
                break
        except Exception:
            continue
    else:
        # Server might still chat without a models list — soft-probe chat endpoint existence via TCP
        try:
            parsed = urlparse(root)
            host_name = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host_name, port), timeout=0.4):
                pass
        except Exception:
            return None
        # Port open but no models API — only accept if preferred model provided
        if not preferred_model:
            return None
        models = [preferred_model]

    model = _pick_best_model(models, preferred_model)
    if not model:
        # LM Studio often needs a model loaded; still register backend for status
        model = preferred_model or "local-model"
    return {
        "name": name,
        "kind": "openai",
        "base": root,
        "v1": v1_base.rstrip("/"),
        "chat_url": v1_base.rstrip("/") + "/chat/completions",
        "models": models,
        "model": model,
    }


def _probe_ollama(host: str | None = None) -> dict | None:
    base = (host or OLLAMA_HOST).rstrip("/")
    models = _ollama_models(base)
    if not models and not OLLAMA_MODEL:
        # Is the daemon up?
        try:
            _http_get_json(f"{base}/api/tags", timeout=0.8)
        except Exception:
            return None
    model = _pick_best_model(models, OLLAMA_MODEL)
    if not model:
        model = OLLAMA_MODEL or "llama3.2"
    return {
        "name": "ollama",
        "kind": "ollama",
        "base": base,
        "v1": base + "/v1",
        "chat_url": base + "/v1/chat/completions",
        "api_chat": base + "/api/chat",
        "models": models,
        "model": model,
    }


def detect_local_llms(force: bool = False) -> list[dict]:
    """Auto-detect LM Studio, Ollama, and optional CATS_LLM_BASE. Cached briefly."""
    now = time.time()
    if (
        not force
        and _LOCAL_LLM_CACHE.get("backends") is not None
        and (now - float(_LOCAL_LLM_CACHE.get("ts") or 0)) < _LOCAL_LLM_CACHE_TTL
    ):
        return list(_LOCAL_LLM_CACHE.get("backends") or [])

    found: list[dict] = []
    seen_bases: set[str] = set()

    def _add(backend: dict | None) -> None:
        if not backend:
            return
        key = _base_no_v1(backend.get("base") or "")
        if key in seen_bases:
            return
        seen_bases.add(key)
        found.append(backend)

    # Explicit custom OpenAI-compatible base first
    if CATS_LLM_BASE:
        _add(_probe_openai_compat("custom-local", CATS_LLM_BASE, CATS_LLM_MODEL or LM_STUDIO_MODEL))

    # LM Studio (default 1234) + common alt ports
    lm_hosts = [LM_STUDIO_HOST]
    for alt in ("http://127.0.0.1:1234", "http://localhost:1234", "http://127.0.0.1:1235"):
        if alt.rstrip("/") not in {h.rstrip("/") for h in lm_hosts}:
            lm_hosts.append(alt)
    for host in lm_hosts:
        _add(_probe_openai_compat("lmstudio", host, LM_STUDIO_MODEL or CATS_LLM_MODEL))

    # Ollama
    for host in (OLLAMA_HOST, "http://127.0.0.1:11434", "http://localhost:11434"):
        _add(_probe_ollama(host))

    # Other common local OpenAI servers (llama.cpp server, etc.)
    for host in ("http://127.0.0.1:8080", "http://127.0.0.1:4891"):
        _add(_probe_openai_compat("local-openai", host, CATS_LLM_MODEL))

    _LOCAL_LLM_CACHE["ts"] = now
    _LOCAL_LLM_CACHE["backends"] = found
    return list(found)


def _llm_via_local(messages: list[dict]) -> dict | None:
    """Chat via auto-detected local LLM (LM Studio / Ollama / custom)."""
    global _LAST_AI_MODEL, _LAST_AI_PROVIDER
    backends = detect_local_llms()
    if not backends:
        return None

    for backend in backends:
        models: list[str] = []
        preferred = backend.get("model") or ""
        if preferred:
            models.append(preferred)
        for m in backend.get("models") or []:
            if m not in models:
                models.append(m)
        # Ollama fallbacks if tags empty but daemon answered
        if backend.get("kind") == "ollama":
            for fallback in ("llama3.2", "llama3.1", "qwen2.5-coder", "mistral", "phi3"):
                if fallback not in models:
                    models.append(fallback)

        seen: set[str] = set()
        for model in models:
            if not model or model in seen:
                continue
            seen.add(model)
            # OpenAI-compatible path (LM Studio, Ollama /v1, llama.cpp)
            try:
                data = _http_json(
                    backend["chat_url"],
                    {
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2,
                        "stream": False,
                    },
                    headers={"Authorization": "Bearer lm-studio"},
                    timeout=180,
                )
                parsed = _parse_chat_response(data)
                if parsed and (parsed.get("content") or parsed.get("tool_calls")):
                    _LAST_AI_MODEL = model
                    _LAST_AI_PROVIDER = backend.get("name") or "local"
                    return parsed
            except Exception:
                pass

            # Native Ollama /api/chat
            if backend.get("kind") == "ollama" and backend.get("api_chat"):
                try:
                    data = _http_json(
                        backend["api_chat"],
                        {
                            "model": model,
                            "messages": messages,
                            "stream": False,
                            "options": {"temperature": 0.2},
                        },
                        timeout=180,
                    )
                    if isinstance(data, dict) and isinstance(data.get("message"), dict):
                        content = data["message"].get("content") or ""
                        if content:
                            _LAST_AI_MODEL = model
                            _LAST_AI_PROVIDER = "ollama"
                            return {"role": "assistant", "content": content, "tool_calls": []}
                    parsed = _parse_chat_response(data)
                    if parsed and (parsed.get("content") or parsed.get("tool_calls")):
                        _LAST_AI_MODEL = model
                        _LAST_AI_PROVIDER = "ollama"
                        return parsed
                except Exception:
                    continue
    return None


# Back-compat alias
def _llm_via_ollama(messages: list[dict]) -> dict | None:
    return _llm_via_local(messages)


def _usable_api_key(key: str) -> bool:
    k = (key or "").strip()
    if len(k) < 20:
        return False
    bad = {
        "sk-free", "free", "not-needed", "any", "none", "null", "undefined",
        "deepseek-v4-only", "your-api-key", "changeme", "placeholder",
    }
    return k.lower() not in bad


def _default_model_for_base(base: str, fallback: str) -> str:
    b = (base or "").lower()
    if "deepseek" in b:
        return "deepseek-chat"
    if "groq" in b:
        return "llama-3.1-8b-instant"
    if "openrouter" in b:
        return "meta-llama/llama-3.2-3b-instruct:free"
    if "pollinations" in b:
        return "openai"
    if "googleapis" in b or "gemini" in b:
        return "gemini-2.0-flash"
    return fallback


def _free_openai_providers() -> list[dict]:
    """Build OpenAI-compatible free/local providers from env (no hardcoded secrets)."""
    providers: list[dict] = []
    cats_key = (
        os.environ.get("CATS_AI_KEY")
        or os.environ.get("POLLINATIONS_API_KEY")
        or os.environ.get("POLLINATIONS_KEY")
        or ""
    ).strip()
    cats_base = (os.environ.get("CATS_AI_BASE") or "").strip().rstrip("/")
    if _usable_api_key(cats_key) and cats_base:
        url = cats_base if cats_base.endswith("/chat/completions") else cats_base + "/chat/completions"
        providers.append({
            "name": "cats-custom",
            "url": url,
            "model": os.environ.get("CATS_AI_MODEL") or _default_model_for_base(cats_base, "openai"),
            "key": cats_key,
        })
    if _usable_api_key(cats_key) and not cats_base:
        providers.append({
            "name": "pollinations-key",
            "url": "https://gen.pollinations.ai/v1/chat/completions",
            "model": os.environ.get("CATS_AI_MODEL") or "openai",
            "key": cats_key,
        })
        providers.append({
            "name": "pollinations-openai",
            "url": "https://text.pollinations.ai/openai",
            "model": os.environ.get("CATS_AI_MODEL") or "openai-fast",
            "key": cats_key,
        })

    groq = (os.environ.get("GROQ_API_KEY") or "").strip()
    if _usable_api_key(groq):
        providers.append({
            "name": "groq",
            "url": "https://api.groq.com/openai/v1/chat/completions",
            "model": os.environ.get("GROQ_MODEL") or "llama-3.1-8b-instant",
            "key": groq,
        })

    openrouter = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if _usable_api_key(openrouter):
        providers.append({
            "name": "openrouter",
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "model": os.environ.get("OPENROUTER_MODEL") or "meta-llama/llama-3.2-3b-instruct:free",
            "key": openrouter,
            "extra_headers": {
                "HTTP-Referer": "https://catide.local",
                "X-Title": "CatIDE",
            },
        })

    gemini = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if _usable_api_key(gemini):
        providers.append({
            "name": "gemini",
            "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "model": os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash",
            "key": gemini,
        })

    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    openai_base = (os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "").strip().rstrip("/")
    if _usable_api_key(openai_key) and openai_base.startswith("http"):
        url = openai_base if openai_base.endswith("/chat/completions") else openai_base + "/chat/completions"
        providers.append({
            "name": "openai-compat",
            "url": url,
            "model": os.environ.get("OPENAI_MODEL") or _default_model_for_base(openai_base, "gpt-4o-mini"),
            "key": openai_key,
        })
    return providers


def _llm_via_openai_compat(messages: list[dict], tools: list | None = None) -> dict | None:
    global _LAST_AI_PROVIDER
    last_err = None
    for ep in _free_openai_providers():
        headers = {
            "Authorization": f"Bearer {ep['key']}",
            "Content-Type": "application/json",
        }
        headers.update(ep.get("extra_headers") or {})
        payload = {
            "model": ep["model"],
            "messages": messages,
            "temperature": 0.2,
        }
        if tools and AI_NATIVE_TOOLS:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        for attempt in range(2):
            try:
                data = _http_json(ep["url"], payload, headers=headers, timeout=90)
                parsed = _parse_chat_response(data)
                if parsed and (parsed.get("content") or parsed.get("tool_calls")):
                    _LAST_AI_PROVIDER = ep["name"]
                    return parsed
                last_err = RuntimeError(str(data)[:200])
                break
            except urllib.error.HTTPError as e:
                last_err = RuntimeError(_http_error_detail(e))
                if e.code in {429, 503, 502}:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last_err = e
                break
    if last_err:
        raise last_err
    return None


def ai_status() -> dict:
    """What free backends are currently usable from this process."""
    local = detect_local_llms(force=True)
    keyed = [p["name"] for p in _free_openai_providers()]
    lm = next((b for b in local if b.get("name") == "lmstudio"), None)
    ol = next((b for b in local if b.get("name") == "ollama"), None)
    active = local[0] if local else None
    return {
        "local": bool(local),
        "local_backends": [
            {
                "name": b.get("name"),
                "base": b.get("base"),
                "model": b.get("model"),
                "models": (b.get("models") or [])[:12],
            }
            for b in local
        ],
        "lmstudio": bool(lm),
        "lmstudio_models": (lm.get("models") or [])[:12] if lm else [],
        "lmstudio_model": (lm or {}).get("model") or "",
        "ollama": bool(ol),
        "ollama_models": (ol.get("models") or [])[:12] if ol else [],
        "ollama_model": (ol or {}).get("model") or "",
        "active_local": {
            "name": (active or {}).get("name"),
            "model": (active or {}).get("model"),
            "base": (active or {}).get("base"),
        } if active else None,
        "keyed_providers": keyed,
        "puter_ui": True,
        "last_provider": _LAST_AI_PROVIDER,
        "last_model": _LAST_AI_MODEL,
        "hint": (
            "Local auto-detect: LM Studio (localhost:1234) and Ollama (localhost:11434). "
            "Or Puter sign-in / free keys: GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, "
            "POLLINATIONS_API_KEY / CATS_AI_KEY."
        ),
    }


def llm_chat(messages: list, tools: list | None = None) -> dict:
    """Route chat to free backends. Returns assistant message dict."""
    global _LAST_AI_PROVIDER
    last_err = None
    clean_messages = _sanitize_messages(messages, tools)

    # 1) Local LLMs — LM Studio / Ollama / CATS_LLM_BASE (auto-detected)
    try:
        got = _llm_via_local(clean_messages)
        if got:
            return got
    except Exception as e:
        last_err = e

    # 2) Env free-tier / custom OpenAI-compatible keys
    try:
        got = _llm_via_openai_compat(clean_messages, tools=tools)
        if got:
            return got
    except Exception as e:
        last_err = e

    # 3) Pollinations anonymous GET (often 402 for coding — try anyway for short chats)
    get_prompt = _flatten_messages_for_get(clean_messages)
    for attempt in range(2):
        try:
            got = _llm_via_get(get_prompt)
            if got:
                _LAST_AI_PROVIDER = "pollinations-get"
                return got
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(_http_error_detail(e))
            if e.code in {429, 503, 502}:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = e
            break

    # 4) Legacy Pollinations POST (usually 402 anonymously — keep as last HTTP try)
    for model in ("openai-fast", "openai"):
        try:
            data = _http_json(
                "https://text.pollinations.ai/openai",
                {
                    "model": model,
                    "messages": clean_messages,
                    "temperature": 0.2,
                    "seed": int(time.time()) % 100000,
                },
                timeout=60,
            )
            parsed = _parse_chat_response(data)
            if parsed and (parsed.get("content") or parsed.get("tool_calls")):
                _LAST_AI_PROVIDER = "pollinations-post"
                return parsed
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(_http_error_detail(e))
            if e.code == 402:
                break
            continue
        except Exception as e:
            last_err = e
            continue

    msg = str(last_err) if last_err else "All free AI backends failed"
    if "402" in msg or "payment required" in msg.lower() or "all free ai" in msg.lower():
        msg = (
            "No local LLM detected and public Pollinations free tier blocks coding (402). "
            "Start LM Studio (Local Server → Start, port 1234) or Ollama (`ollama run llama3.2`), "
            "or use Puter sign-in / a free key (GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY)."
        )
    raise RuntimeError(msg)


def normalize_tool_calls(msg: dict) -> list[dict]:
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args
        calls.append({
            "id": tc.get("id") or f"call_{len(calls)}",
            "name": fn.get("name") or "",
            "arguments": args if isinstance(args, dict) else {},
        })
    if not calls:
        calls = parse_text_tool_calls(msg.get("content") or "")
    return calls


def run_cursor_agent(body: dict) -> dict:
    mode = (body.get("mode") or "agent").lower()
    if mode not in {"agent", "ask", "edit", "plan", "manual"}:
        mode = "agent"
    user_message = body.get("message") or ""
    history = body.get("history") or []
    active_path = body.get("active_path")
    open_files = body.get("open_files") or {}

    # Prefer text tool protocol — native function-calling hits 402 on free tier
    tools = tool_defs_for_mode(mode) if AI_NATIVE_TOOLS else None
    messages: list[dict] = [
        {"role": "system", "content": agent_system_prompt(mode, active_path, open_files)},
    ]
    if isinstance(history, list):
        for h in history[-10:]:
            if isinstance(h, dict) and h.get("role") in {"user", "assistant"} and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    tail = messages[-2:] if len(messages) >= 2 else messages
    if not any(m.get("role") == "user" and m.get("content") == user_message for m in tail):
        messages.append({"role": "user", "content": user_message})

    steps: list[dict] = []
    files_changed: list[str] = []
    terminal_chunks: list[str] = []
    final_text = ""

    for _ in range(AGENT_MAX_STEPS):
        msg = llm_chat(messages, tools=tools)
        calls = normalize_tool_calls(msg)

        if not calls:
            final_text = strip_tool_markup(msg.get("content") or "") or (msg.get("content") or "").strip()
            break

        # Keep assistant turn for native tool protocol
        assistant_msg = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            assistant_msg["tool_calls"] = msg["tool_calls"]
        messages.append(assistant_msg)

        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name") or ""
            args = call.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            result, changed, term = execute_tool(name, args, mode)
            result = "" if result is None else str(result)
            files_changed.extend(changed or [])
            if term:
                terminal_chunks.append(term)
            steps.append({
                "name": name,
                "args": args,
                "result": result[:2000],
            })
            if msg.get("tool_calls"):
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{len(steps)}",
                    "content": result[:12000],
                })
            else:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Tool result [{name}]:\n{result[:12000]}\n\n"
                        "Continue. If done, summarize without more tools."
                    ),
                })
        # next agent iteration
        continue

    if not final_text:
        # max steps reached without a final text-only turn
        messages.append({
            "role": "user",
            "content": "Stop using tools. Summarize what you accomplished.",
        })
        try:
            msg = llm_chat(messages, tools=None)
            final_text = (msg.get("content") or "").strip()
        except Exception:
            final_text = ""

    if not final_text:
        if steps:
            final_text = f"Completed {len(steps)} tool step(s)."
            if files_changed:
                final_text += " Updated: " + ", ".join(dict.fromkeys(files_changed))
        else:
            final_text = "No response from model."

    # dedupe files
    files_changed = list(dict.fromkeys(files_changed))
    return {
        "content": final_text,
        "steps": steps,
        "files_changed": files_changed,
        "terminal_log": "\n".join(terminal_chunks),
        "mode": mode,
    }


def chat_completion(messages: list) -> str:
    msg = llm_chat(messages, tools=None)
    return (msg.get("content") or "").strip()


def find_free_port(start: int = PORT) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    return start


def ensure_webview():
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        pass
    print("Installing pywebview for standalone window…")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pywebview", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import webview  # noqa: F401
        return True
    except Exception as e:
        print(f"Could not install pywebview: {e}")
        return False


class CatsBridge:
    """JS ↔ Python bridge for native dialogs."""

    def open_folder(self):
        import webview

        wins = getattr(webview, "windows", None) or []
        window = wins[0] if wins else _WINDOW
        if window is None:
            return None
        try:
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            return None
        if not result:
            return None
        # pywebview may return a tuple/list of paths
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return str(result)


def run_standalone(url: str, server: ThreadingHTTPServer) -> None:
    """Native desktop window (WKWebView / system webview) — no Chrome/browser."""
    global _WINDOW
    import webview

    _WINDOW = webview.create_window(
        APP_NAME,
        url,
        width=1440,
        height=900,
        min_size=(900, 600),
        confirm_close=False,
        background_color="#1e1e1e",
        js_api=CatsBridge(),
    )
    try:
        webview.start(private_mode=True)
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    if not ensure_webview():
        print("CatIDE0.1 needs pywebview for a standalone window.")
        print("Install with: python3 -m pip install pywebview")
        sys.exit(1)

    port = find_free_port()
    server = ThreadingHTTPServer((HOST, port), CatIDEHandler)
    url = f"http://{HOST}:{port}/"

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print(f"{APP_NAME} standalone window")
    print("Workspace:", WORKSPACE)
    st = ai_status()
    active = st.get("active_local") or {}
    local_label = "off"
    if active.get("name"):
        local_label = f"{active.get('name')}:{active.get('model') or '?'}"
    print(
        "AI routes: local", local_label,
        "· LM Studio", "ON" if st.get("lmstudio") else "off",
        "· Ollama", "ON" if st.get("ollama") else "off",
        "· Puter UI · keyed:", ",".join(st.get("keyed_providers") or []) or "none",
    )
    print("Tip: LM Studio Local Server :1234 · `ollama run llama3.2` · Puter sign-in · or GROQ_API_KEY")

    run_standalone(url, server)


if __name__ == "__main__":
    main()
