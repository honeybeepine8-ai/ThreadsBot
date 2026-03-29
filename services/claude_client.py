"""Claude client using Claude Code CLI (claude -p) for Max subscription usage."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from core.logger import get_logger

logger = get_logger("claude_client")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


class ClaudeClient:
    """Invoke Claude via the Claude Code CLI (``claude -p``).

    This avoids the need for an ``ANTHROPIC_API_KEY`` by leveraging the
    user's existing Claude Max/Pro subscription through the CLI.
    """

    def __init__(self) -> None:
        self.config = self._load_config()
        self.max_tokens_write: int = self.config.get("max_tokens_write", 600)
        self.max_tokens_evaluate: int = self.config.get("max_tokens_evaluate", 200)

        logger.info("ClaudeClient initialised (CLI mode via 'claude -p')")

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def generate_post(self, prompt: str, system_prompt: str | None = None) -> str:
        """Generate a Threads post via Claude CLI.

        Args:
            prompt: The user-facing prompt describing what to write.
            system_prompt: Optional system-level instruction prepended to prompt.

        Returns:
            The generated post text.
        """
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        logger.debug("generate_post: prompt length=%d", len(full_prompt))
        text = self._call_cli(full_prompt, max_tokens=self.max_tokens_write)
        logger.debug("generate_post: response length=%d", len(text))
        return text

    def evaluate_quality(self, content: str, criteria: str) -> dict:
        """Evaluate content quality via Claude CLI.

        Args:
            content: The text to evaluate.
            criteria: Scoring criteria / rubric to apply.

        Returns:
            A dict with ``scores``, ``average``, and ``feedback`` keys.
        """
        system_prompt = (
            "You are a content quality evaluator. "
            "Evaluate the given content based on the provided criteria. "
            "Return your evaluation as a JSON object. "
            "The scores and feedback keys must match the criteria provided. "
            "Return ONLY valid JSON, no markdown fences or extra text."
        )

        user_prompt = (
            f"## Content to evaluate\n{content}\n\n"
            f"## Scoring criteria\n{criteria}"
        )

        full_prompt = f"{system_prompt}\n\n{user_prompt}"

        logger.debug("evaluate_quality via CLI")
        raw_text = self._call_cli(full_prompt, max_tokens=self.max_tokens_evaluate)

        try:
            result: dict = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse evaluator JSON, attempting extraction: %s", raw_text[:200])
            start = raw_text.find("{")
            end = raw_text.rfind("}") + 1
            if start != -1 and end > start:
                result = json.loads(raw_text[start:end])
            else:
                raise

        logger.debug("evaluate_quality: scores=%s", result.get("scores", {}))
        return result

    def analyze(self, prompt: str, data: str) -> str:
        """Run a general-purpose analysis via Claude CLI.

        Args:
            prompt: Instructions for the analysis.
            data: The data / context to analyze.

        Returns:
            The analysis result as text.
        """
        user_message = f"## Task\n{prompt}\n\n## Data\n{data}"

        logger.debug("analyze: data length=%d", len(data))
        text = self._call_cli(user_message, max_tokens=self.max_tokens_write)
        logger.debug("analyze: response length=%d", len(text))
        return text

    def prompt(self, text: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        """Send a raw prompt and return the response text.

        Args:
            text: The full prompt to send.
            max_tokens: Maximum tokens for the response.
            timeout: Subprocess timeout in seconds.

        Returns:
            The raw response text.
        """
        return self._call_cli(text, max_tokens=max_tokens, timeout=timeout)

    # ------------------------------------------------------------------
    # CLI invocation
    # ------------------------------------------------------------------

    @staticmethod
    def _call_cli(prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        """Call ``claude -p`` as a subprocess and return the response text.

        Retries once on timeout or non-zero exit code before raising.

        Args:
            prompt: The full prompt to send.
            max_tokens: Maximum tokens for the response (passed via --max-tokens).
            timeout: Subprocess timeout in seconds.

        Returns:
            The CLI's stdout output (stripped).

        Raises:
            RuntimeError: If retries are exhausted.
        """
        import time as _time

        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format", "text",
        ]

        env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
        max_attempts = 2  # 1回リトライ

        for attempt in range(1, max_attempts + 1):
            logger.debug(
                "Calling claude CLI (prompt length=%d, attempt %d/%d)",
                len(prompt), attempt, max_attempts,
            )

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Claude CLI timed out after %ds (attempt %d/%d)",
                    timeout, attempt, max_attempts,
                )
                if attempt < max_attempts:
                    _time.sleep(5)
                    continue
                raise RuntimeError(f"Claude CLI timed out after {timeout}s (exhausted {max_attempts} attempts)")

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                logger.warning(
                    "Claude CLI failed (exit %d, attempt %d/%d): %s",
                    result.returncode, attempt, max_attempts, stderr,
                )
                if attempt < max_attempts:
                    _time.sleep(5)
                    continue
                raise RuntimeError(f"Claude CLI error (exit {result.returncode}): {stderr}")

            output = result.stdout.decode("utf-8", errors="replace").strip()
            if not output:
                logger.warning("Claude CLI returned empty output (attempt %d/%d)", attempt, max_attempts)
                if attempt < max_attempts:
                    _time.sleep(3)
                    continue
                raise RuntimeError("Claude CLI returned empty output")

            return output

        raise RuntimeError("Claude CLI: unreachable")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config() -> dict[str, Any]:
        """Load the ``claude`` section from *settings.yaml*."""
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("claude", {})
