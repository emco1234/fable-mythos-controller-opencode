"""
Real OpenCode CLI adapter.

Spawns a subagent via `opencode run --agent <name> --message <prompt>`.
Optionally uses `--format json` to capture structured events.

Verified against `opencode run --help`:
  - `opencode run <message>` runs a single message headless
  - `--agent <name>` selects the agent
  - `--format json` emits structured JSON events
  - `--session <id>` reuses a session (for resume)

The adapter:
  1. Runs `opencode run --agent <name> --message <prompt> --format json`
  2. Parses JSON events from stdout
  3. Writes captured events + text to .transcripts/<agent>-<ts>.md
  4. Returns the transcript path
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from controller import PlatformAdapter


TRANSCRIPT_DIR = Path(".transcripts")
OPENCODE_BIN_ENV = "RELIABILITY_OPENCODE_BIN"


def _find_opencode() -> str | None:
    custom = os.environ.get(OPENCODE_BIN_ENV)
    if custom and Path(custom).exists():
        return custom
    return shutil.which("opencode")


class OpenCodeAdapter(PlatformAdapter):
    """Spawn an OpenCode subagent via the `opencode run` headless command."""

    def __init__(self, default_cwd: Path | None = None, json_format: bool = True):
        self.default_cwd = default_cwd
        self.json_format = json_format
        self.last_invocation: dict[str, Any] = {}

    async def spawn(self, agent_name: str, prompt: str) -> str:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        transcript_path = TRANSCRIPT_DIR / f"{agent_name}-{timestamp}.md"

        opencode_bin = _find_opencode()
        if not opencode_bin:
            return self._write_stub(transcript_path, agent_name, prompt,
                                    reason="opencode CLI not found in PATH")

        cmd: list[str] = [
            opencode_bin, "run",
            "--agent", agent_name,
            "--message", prompt,
        ]
        if self.json_format:
            cmd.extend(["--format", "json"])

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.default_cwd) if self.default_cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=300)
            duration_ms = int((time.monotonic() - start) * 1000)
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        except (FileNotFoundError, OSError) as e:
            return self._write_stub(transcript_path, agent_name, prompt,
                                    reason=f"spawn failed: {e!r}")
        except asyncio.TimeoutError:
            return self._write_stub(transcript_path, agent_name, prompt,
                                    reason="opencode run timed out after 300s")

        # If --format json was used, parse events
        events: list[dict[str, Any]] = []
        text_blocks: list[str] = []
        if self.json_format:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    events.append(ev)
                    if isinstance(ev, dict):
                        # Try common keys for assistant text
                        for key in ("text", "content", "message"):
                            if key in ev and isinstance(ev[key], str):
                                text_blocks.append(ev[key])
                except json.JSONDecodeError:
                    text_blocks.append(line)
        else:
            text_blocks.append(stdout)

        transcript_path.write_text(
            f"# OpenCode transcript: {agent_name}\n\n"
            f"- timestamp: {timestamp}\n"
            f"- duration_ms: {duration_ms}\n"
            f"- exit_code: {proc.returncode}\n"
            f"- command: {' '.join(cmd)}\n"
            f"- events: {len(events)}\n\n"
            f"## assistant text\n\n"
            + "\n".join(text_blocks[:200])
            + "\n\n## prompt\n\n```\n{prompt}\n```\n".format(prompt=prompt)
            + (f"\n## stderr\n\n```\n{stderr[:5000]}\n```\n" if stderr else ""),
            encoding="utf-8",
        )

        self.last_invocation = {
            "agent": agent_name,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "events": len(events),
        }

        if proc.returncode != 0:
            transcript_path.write_text(
                transcript_path.read_text(encoding="utf-8") +
                f"\n\n> NOTE: opencode run exited with non-zero status ({proc.returncode}).\n",
                encoding="utf-8",
            )
        return str(transcript_path)

    @staticmethod
    def _write_stub(path: Path, agent_name: str, prompt: str, reason: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Transcript STUB: {agent_name}\n\n"
            f"Reason: {reason}\n\n"
            f"Prompt that would have been sent:\n\n```\n{prompt}\n```\n",
            encoding="utf-8",
        )
        return str(path)