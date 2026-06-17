"""LLM compatibility helpers for OpenAI-compatible streaming APIs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Optional

from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException


# BEGIN provider-compat: detect provider content-filter failures
def is_sensitive_words_error(exc: BaseException) -> bool:
    """Return whether an upstream provider rejected the request for content policy."""

    return "sensitive_words_detected" in str(exc).lower()
# END provider-compat


# BEGIN provider-compat: tolerate empty streaming chunks from OpenAI-compatible providers
class RobustHelloAgentsLLM(HelloAgentsLLM):
    """HelloAgentsLLM variant that tolerates empty streaming chunks."""

    def think(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
    ) -> Iterator[str]:
        """Stream model output while skipping provider heartbeat chunks."""

        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=self.max_tokens,
                stream=True,
            )

            print("✅ 大语言模型响应成功:")
            for chunk in response:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue

                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta else None
                if content:
                    print(content, end="", flush=True)
                    yield content
            print()

        except Exception as exc:
            print(f"❌ 调用LLM API时发生错误: {exc}")
            raise HelloAgentsException(f"LLM调用失败: {str(exc)}") from exc
# END provider-compat
