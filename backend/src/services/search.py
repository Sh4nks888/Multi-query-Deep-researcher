"""Search dispatch helpers leveraging HelloAgents SearchTool."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Tuple

from hello_agents.tools import SearchTool

from config import Configuration
from utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
)

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000
SAFE_CONTEXT_CHARS_PER_SOURCE = 240
_GLOBAL_SEARCH_TOOL = SearchTool(backend="hybrid")


def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str]:
    """Execute configured search backend and normalise response payload."""

    search_api = get_config_value(config.search_api)#读取搜索后端

    try: #调用搜索工具
        raw_response = _GLOBAL_SEARCH_TOOL.run(
            {
                "input": query,
                "backend": search_api,
                "mode": "structured",
                "fetch_full_page": config.fetch_full_page,
                "max_results": 5,
                "max_tokens_per_source": MAX_TOKENS_PER_SOURCE,
                "loop_count": loop_count,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        notice = f"⚠️ {search_api} 搜索失败：{exc}"
        logger.exception("Search backend %s failed: %s", search_api, exc)
        return (
            {
                "results": [],
                "backend": search_api,
                "answer": None,
                "notices": [notice],
            },
            [notice],
            None,
            search_api,
        )

    if isinstance(raw_response, str):
        notices = [raw_response]
        logger.warning("Search backend %s returned text notice: %s", search_api, raw_response)
        payload: dict[str, Any] = {
            "results": [],
            "backend": search_api,
            "answer": None,
            "notices": notices,
        }
    else:
        payload = raw_response
        notices = list(payload.get("notices") or [])

    backend_label = str(payload.get("backend") or search_api)
    answer_text = payload.get("answer")
    results = payload.get("results", [])

    if notices:
        for notice in notices:
            logger.info("Search notice (%s): %s", backend_label, notice)

    logger.info(
        "Search backend=%s resolved_backend=%s answer=%s results=%s",
        search_api,
        backend_label,
        bool(answer_text),
        len(results),
    )

    return payload, notices, answer_text, backend_label


# BEGIN retrieval-confidence: multi-query retrieval and result fusion
def build_query_variants(query: str, config: Configuration) -> list[str]:
    """Build deterministic query variants without adding extra LLM calls."""

    base_query = " ".join((query or "").split())
    if not base_query:
        return []

    max_queries = max(1, int(config.multi_query_count or 1))
    variants = [base_query]

    if max_queries > 1:
        variants.append(f"{base_query} 最新进展 关键证据")

    if max_queries > 2:
        if _contains_cjk(base_query):
            variants.append(f"{base_query} best practices evaluation benchmark")
        else:
            variants.append(f"{base_query} latest research evidence benchmark")

    if max_queries > 3:
        variants.append(f"{base_query} production challenges limitations")

    unique_variants: list[str] = []
    seen = set()
    for variant in variants:
        normalized = " ".join(variant.split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            unique_variants.append(normalized)

    return unique_variants[:max_queries]


def resolve_query_variants(
    query: str,
    config: Configuration,
    planned_variants: Optional[list[str]] = None,
) -> list[str]:
    """Use planner-generated variants first, then fill gaps with deterministic fallbacks."""

    max_queries = max(1, int(config.multi_query_count or 1))
    candidates: list[str] = []

    if planned_variants:
        candidates.extend(planned_variants)

    candidates.extend(build_query_variants(query, config))

    variants: list[str] = []
    seen = set()
    for candidate in candidates:
        normalized = " ".join(str(candidate or "").split())
        if not normalized:
            continue
        if len(normalized) > 160:
            normalized = normalized[:160].strip()
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        variants.append(normalized)
        if len(variants) >= max_queries:
            break

    return variants or [" ".join((query or "").split())]


def dispatch_multi_search(
    query: str,
    config: Configuration,
    loop_count: int,
    query_variants: Optional[list[str]] = None,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str, list[str]]:
    """Execute one or more search queries and merge deduplicated results."""

    if not config.enable_multi_query_retrieval:
        payload, notices, answer_text, backend = dispatch_search(query, config, loop_count)
        return payload, notices, answer_text, backend, [query]

    query_variants = resolve_query_variants(query, config, query_variants)

    merged_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    notices: list[str] = []
    answer_parts: list[str] = []
    backend_labels: list[str] = []

    for index, variant in enumerate(query_variants): #串行搜索
        payload, query_notices, answer_text, backend = dispatch_search(
            variant,
            config,
            loop_count + index,
        )
        notices.extend(query_notices)
        backend_labels.append(backend)

        if answer_text:
            answer_parts.append(str(answer_text).strip())

        for result in (payload or {}).get("results", []):
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            result_copy = dict(result)
            result_copy.setdefault("query_variant", variant)
            merged_results.append(result_copy)

    max_results = max(1, int(config.max_merged_search_results or 1))
    merged_results = merged_results[:max_results]
    backend_label = "+".join(dict.fromkeys(backend_labels)) or get_config_value(config.search_api)
    answer_text = "\n\n".join(dict.fromkeys(answer_parts)) if answer_parts else None

    if len(query_variants) > 1:
        notices.append(
            f"Multi-query Retrieval 已执行 {len(query_variants)} 个查询，合并 {len(merged_results)} 个去重来源。"
        )

    logger.info(
        "Multi-query retrieval queries=%s merged_results=%s backend=%s",
        len(query_variants),
        len(merged_results),
        backend_label,
    )

    return (
        {
            "results": merged_results,
            "backend": backend_label,
            "answer": answer_text,
            "notices": notices,
            "query_variants": query_variants,
        },
        notices,
        answer_text,
        backend_label,
        query_variants,
    )


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))
# END retrieval-confidence


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: Optional[str],
    config: Configuration,
) -> tuple[str, str]:
    """Build structured context and source summary for downstream agents."""

    sources_summary = format_sources(search_result)
    context = deduplicate_and_format_sources(
        search_result or {"results": []},
        max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
        fetch_full_page=config.fetch_full_page,
    )

    if answer_text:
        context = f"AI直接答案：\n{answer_text}\n\n{context}"

    return sources_summary, context


# BEGIN provider-compat: compact context for provider content-filter retry
def prepare_safe_research_context(
    search_result: dict[str, Any] | None,
    *,
    max_chars_per_source: int = SAFE_CONTEXT_CHARS_PER_SOURCE,
    include_snippets: bool = False,
) -> str:
    """Build a compact context for retrying model calls with less page noise."""

    if not search_result:
        return "暂无可用检索材料。"

    results = search_result.get("results", [])
    unique_sources: dict[str, dict[str, Any]] = {}
    for source in results:
        url = source.get("url")
        if not url or url in unique_sources:
            continue
        unique_sources[url] = source

    formatted_parts = ["以下为精简检索材料，仅保留来源标题与链接；请基于这些公开来源谨慎总结。\n"]
    for index, source in enumerate(unique_sources.values(), start=1):
        title = str(source.get("title") or source.get("url") or f"来源 {index}").strip()
        url = str(source.get("url") or "").strip()
        snippet = str(source.get("content") or "").strip()
        if len(snippet) > max_chars_per_source:
            snippet = f"{snippet[:max_chars_per_source]}..."

        formatted_parts.append(f"来源 {index}: {title}\n")
        formatted_parts.append(f"URL: {url}\n")
        if include_snippets and snippet:
            formatted_parts.append(f"摘要: {snippet}\n")
        formatted_parts.append("\n")

    return "".join(formatted_parts).strip()
# END provider-compat
