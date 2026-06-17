"""Orchestrator coordinating the deep research workflow."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Semaphore, Thread
from typing import Any, Callable, Generator, Iterator

from hello_agents import ToolAwareSimpleAgent
from hello_agents.tools import ToolRegistry
from hello_agents.tools.builtin.note_tool import NoteTool

from config import Configuration
from prompts import (
    report_writer_instructions,
    task_summarizer_instructions,
    todo_planner_system_prompt,
)
from models import SummaryState, SummaryStateOutput, TodoItem
from services.planner import PlanningService
from services.reporter import ReportingService
from services.search import (
    dispatch_multi_search,
    prepare_research_context,
    prepare_safe_research_context,
)
from services.llm import RobustHelloAgentsLLM, is_sensitive_words_error
from services.summarizer import SummarizationService
from services.tool_events import ToolCallTracker

logger = logging.getLogger(__name__)


class DeepResearchAgent:
    """Coordinator orchestrating TODO-based research workflow using HelloAgents."""

    def __init__(self, config: Configuration | None = None) -> None:
        """Initialise the coordinator with configuration and shared tools."""
        self.config = config or Configuration.from_env()
        self.llm = self._init_llm()

        self.note_tool = ( #笔记工具
            NoteTool(workspace=self.config.notes_workspace)
            if self.config.enable_notes
            else None
        )
        self.tools_registry: ToolRegistry | None = None #工具注册表
        if self.note_tool:
            registry = ToolRegistry()
            registry.register_tool(self.note_tool)
            self.tools_registry = registry

        self._tool_tracker = ToolCallTracker(
            self.config.notes_workspace if self.config.enable_notes else None
        )
        self._tool_event_sink_enabled = False
        self._state_lock = Lock()
        # BEGIN provider-compat: avoid overwhelming OpenAI-compatible streaming providers
        self._worker_semaphore = Semaphore(max(1, self.config.max_concurrent_tasks))
        # END provider-compat

        self.todo_agent = self._create_tool_aware_agent( #创建研究规划agent
            name="研究规划专家",
            system_prompt=todo_planner_system_prompt.strip(),
            enable_tool_calling=False,
        )
        self.report_agent = self._create_tool_aware_agent( #创建报告agent
            name="报告撰写专家",
            system_prompt=report_writer_instructions.strip(),
        )

        self._summarizer_factory: Callable[[], ToolAwareSimpleAgent] = lambda: self._create_tool_aware_agent(  # noqa: E501
            name="任务总结专家",
            system_prompt=task_summarizer_instructions.strip(),
        )

        self.planner = PlanningService(self.todo_agent, self.config)
        self.summarizer = SummarizationService(self._summarizer_factory, self.config)
        self.reporting = ReportingService(self.report_agent, self.config)
        self._last_search_notices: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def _init_llm(self) -> RobustHelloAgentsLLM:
        """Instantiate HelloAgentsLLM following configuration preferences."""
        llm_kwargs: dict[str, Any] = {"temperature": 0.0}

        model_id = self.config.llm_model_id or self.config.local_llm
        if model_id:
            llm_kwargs["model"] = model_id

        provider = (self.config.llm_provider or "").strip()
        if provider:
            llm_kwargs["provider"] = provider

        if provider == "ollama":
            llm_kwargs["base_url"] = self.config.sanitized_ollama_url()
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif provider == "lmstudio":
            llm_kwargs["base_url"] = self.config.lmstudio_base_url
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key
        else:
            if self.config.llm_base_url:
                llm_kwargs["base_url"] = self.config.llm_base_url
            if self.config.llm_api_key:
                llm_kwargs["api_key"] = self.config.llm_api_key

        return RobustHelloAgentsLLM(**llm_kwargs)

    def _create_tool_aware_agent(
        self,
        *,
        name: str,
        system_prompt: str,#模型提示词，定义了 Agent 的角色、行为规范和输出格式
        enable_tool_calling: bool = True,
    ) -> ToolAwareSimpleAgent:
        """Instantiate a ToolAwareSimpleAgent sharing tool registry and tracker."""
        return ToolAwareSimpleAgent(
            name=name,
            llm=self.llm,
            system_prompt=system_prompt,
            enable_tool_calling=enable_tool_calling and self.tools_registry is not None,
            tool_registry=self.tools_registry if enable_tool_calling else None,
            tool_call_listener=self._tool_tracker.record,
        )

    def _set_tool_event_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Enable or disable immediate tool event callbacks."""
        self._tool_event_sink_enabled = sink is not None
        self._tool_tracker.set_event_sink(sink)

    def run(self, topic: str) -> SummaryStateOutput:
        """Execute the research workflow and return the final report."""
        state = SummaryState(research_topic=topic)
        state.todo_items = self.planner.plan_todo_list(state)
        self._drain_tool_events(state)

        if not state.todo_items:
            logger.info("No TODO items generated; falling back to single task")
            state.todo_items = [self.planner.create_fallback_task(state)]

        for task in state.todo_items:
            self._execute_task(state, task, emit_stream=False)

        report = self.reporting.generate_report(state)
        self._drain_tool_events(state)
        state.structured_report = report
        state.running_summary = report
        self._persist_final_report(state, report)

        return SummaryStateOutput(
            running_summary=report,
            report_markdown=report,
            todo_items=state.todo_items,
        )

    def run_stream(self, topic: str) -> Iterator[dict[str, Any]]:
        """Execute the workflow yielding incremental progress events."""
        state = SummaryState(research_topic=topic) #用户的输入先放进状态对象，状态对象很重要，几乎包含了所有的信息，包括用户问题，工具流程，LLM回复等
        logger.debug("Starting streaming research: topic=%s", topic)
        yield {"type": "status", "message": "初始化研究流程"}

        state.todo_items = self.planner.plan_todo_list(state) #将当前状态传入给planner
        for event in self._drain_tool_events(state, step=0):
            yield event
        if not state.todo_items:
            state.todo_items = [self.planner.create_fallback_task(state)]

        channel_map: dict[int, dict[str, Any]] = {}
        for index, task in enumerate(state.todo_items, start=1):
            token = f"task_{task.id}"
            task.stream_token = token
            channel_map[task.id] = {"step": index, "token": token}

        yield {
            "type": "todo_list",
            "tasks": [self._serialize_task(t) for t in state.todo_items],
            "step": 0,
        }

        event_queue: Queue[dict[str, Any]] = Queue()

        def enqueue(
            event: dict[str, Any],
            *,
            task: TodoItem | None = None,
            step_override: int | None = None,
        ) -> None:
            payload = dict(event)
            target_task_id = payload.get("task_id")
            if task is not None:
                target_task_id = task.id
                payload["task_id"] = task.id

            channel = channel_map.get(target_task_id) if target_task_id is not None else None
            if channel:
                payload.setdefault("step", channel["step"])
                payload["stream_token"] = channel["token"]
            if step_override is not None:
                payload["step"] = step_override
            event_queue.put(payload)

        def tool_event_sink(event: dict[str, Any]) -> None:
            enqueue(event)

        self._set_tool_event_sink(tool_event_sink)

        threads: list[Thread] = []

        def worker(task: TodoItem, step: int) -> None:
            try:
                with self._worker_semaphore:
                    enqueue(
                        {
                            "type": "task_status",
                            "task_id": task.id,
                            "status": "in_progress",
                            "title": task.title,
                            "intent": task.intent,
                            "note_id": task.note_id,
                            "note_path": task.note_path,
                        },
                        task=task,
                    )

                    for event in self._execute_task(state, task, emit_stream=True, step=step):
                        enqueue(event, task=task)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception("Task execution failed", exc_info=exc)
                task.status = "failed"
                if not task.summary:
                    task.summary = f"任务执行失败：{exc}"
                enqueue(
                    {
                        "type": "task_status",
                        "task_id": task.id,
                        "status": "failed",
                        "detail": str(exc),
                        "summary": task.summary,
                        "title": task.title,
                        "intent": task.intent,
                        "note_id": task.note_id,
                        "note_path": task.note_path,
                    },
                    task=task,
                )
            finally:
                enqueue({"type": "__task_done__", "task_id": task.id})

        for task in state.todo_items: 
            step = channel_map.get(task.id, {}).get("step", 0)
            thread = Thread(target=worker, args=(task, step), daemon=True)
            threads.append(thread)
            thread.start()

        active_workers = len(state.todo_items)
        finished_workers = 0

        try:
            while finished_workers < active_workers:
                event = event_queue.get()
                if event.get("type") == "__task_done__":
                    finished_workers += 1
                    continue
                yield event

            while True:
                try:
                    event = event_queue.get_nowait()
                except Empty:
                    break
                if event.get("type") != "__task_done__":
                    yield event
        finally:
            self._set_tool_event_sink(None)
            for thread in threads:
                thread.join()

        report = self.reporting.generate_report(state)
        final_step = len(state.todo_items) + 1
        for event in self._drain_tool_events(state, step=final_step):
            yield event
        state.structured_report = report
        state.running_summary = report

        note_event = self._persist_final_report(state, report)
        if note_event:
            yield note_event

        yield {
            "type": "final_report",
            "report": report,
            "note_id": state.report_note_id,
            "note_path": state.report_note_path,
        }
        yield {"type": "done"}

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------
    def _execute_task(
        self,
        state: SummaryState,
        task: TodoItem,
        *,
        emit_stream: bool,
        step: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run search + summarization for a single task."""
        task.status = "in_progress"

        search_result, notices, answer_text, backend, query_variants = dispatch_multi_search(
            task.query,
            self.config,
            state.research_loop_count,
            task.query_variants,
        )
        self._last_search_notices = notices
        task.notices = notices
        # BEGIN retrieval-confidence: keep retrieval metadata on the task
        task.query_variants = query_variants
        task.search_result_count = len((search_result or {}).get("results", []))
        # END retrieval-confidence

        if emit_stream:
            for event in self._drain_tool_events(state, step=step):
                yield event
        else:
            self._drain_tool_events(state)

        if notices and emit_stream:
            for notice in notices:
                if notice:
                    yield {
                        "type": "status",
                        "message": notice,
                        "task_id": task.id,
                        "step": step,
                    }

        if not search_result or not search_result.get("results"):
            task.status = "skipped"
            self._score_task_confidence(task)
            if emit_stream:
                for event in self._drain_tool_events(state, step=step):
                    yield event
                yield {
                    "type": "task_status",
                    "task_id": task.id,
                    "status": "skipped",
                    "title": task.title,
                    "intent": task.intent,
                    "note_id": task.note_id,
                    "note_path": task.note_path,
                    "query_variants": task.query_variants,
                    "search_result_count": task.search_result_count,
                    "confidence_score": task.confidence_score,
                    "confidence_label": task.confidence_label,
                    "confidence_reason": task.confidence_reason,
                    "step": step,
                }
            else:
                self._drain_tool_events(state)
            return
        else:
            if not emit_stream:
                self._drain_tool_events(state)

        sources_summary, context = prepare_research_context(
            search_result,
            answer_text,
            self.config,
        )

        task.sources_summary = sources_summary

        with self._state_lock:
            state.web_research_results.append(context)
            state.sources_gathered.append(sources_summary)
            state.research_loop_count += 1

        summary_text: str | None = None

        if emit_stream:
            for event in self._drain_tool_events(state, step=step):
                yield event
            yield {
                "type": "sources",
                "task_id": task.id,
                "latest_sources": sources_summary,
                "raw_context": context,
                "step": step,
                "backend": backend,
                "query_variants": task.query_variants,
                "search_result_count": task.search_result_count,
                "note_id": task.note_id,
                "note_path": task.note_path,
            }

            # BEGIN provider-compat: retry with compact context when provider filters page text
            try:
                summary_text = yield from self._stream_task_summary(
                    state,
                    task,
                    context,
                    step=step,
                )
            except Exception as exc:
                if not is_sensitive_words_error(exc):
                    raise

                notice = "模型服务商回复受阻，采用智能降级回复。"
                task.fallback_used = True
                task.fallback_reason = "sensitive_words_detected"
                task.fallback_stage = "compact_retry_pending"
                task.notices.append(notice)
                logger.warning(
                    "Sensitive words detected for task %s (%s); retrying with compact context",
                    task.id,
                    task.title,
                )
                yield {
                    "type": "status",
                    "message": notice,
                    "task_id": task.id,
                    "step": step,
                    "fallback_used": task.fallback_used,
                    "fallback_reason": task.fallback_reason,
                    "fallback_stage": task.fallback_stage,
                }

                safe_context = prepare_safe_research_context(search_result)
                try:
                    summary_text = yield from self._stream_task_summary(
                        state,
                        task,
                        safe_context,
                        step=step,
                    )
                except Exception as retry_exc:
                    if not is_sensitive_words_error(retry_exc):
                        raise

                    fallback_notice = "精简上下文仍被模型服务商拦截，已生成基于来源列表的保守总结。"
                    task.fallback_stage = "local_summary"
                    task.notices.append(fallback_notice)
                    logger.warning(
                        "Compact context retry was also filtered for task %s (%s); using local fallback summary",
                        task.id,
                        task.title,
                    )
                    yield {
                        "type": "status",
                        "message": fallback_notice,
                        "task_id": task.id,
                        "step": step,
                        "fallback_used": task.fallback_used,
                        "fallback_reason": task.fallback_reason,
                        "fallback_stage": task.fallback_stage,
                    }
                    summary_text = self._build_provider_fallback_summary(task, sources_summary)
                else:
                    task.fallback_stage = "compact_retry_success"
                    logger.info(
                        "Compact context retry succeeded for task %s (%s)",
                        task.id,
                        task.title,
                    )
            # END provider-compat
        else:
            # BEGIN provider-compat: retry non-streaming runs with compact context
            try:
                summary_text = self.summarizer.summarize_task(state, task, context)
            except Exception as exc:
                if not is_sensitive_words_error(exc):
                    raise
                task.fallback_used = True
                task.fallback_reason = "sensitive_words_detected"
                task.fallback_stage = "compact_retry_pending"
                task.notices.append("模型服务商回复受阻，采用智能降级回复。")
                logger.warning(
                    "Sensitive words detected for task %s (%s); retrying with compact context",
                    task.id,
                    task.title,
                )
                safe_context = prepare_safe_research_context(search_result)
                try:
                    summary_text = self.summarizer.summarize_task(state, task, safe_context)
                except Exception as retry_exc:
                    if not is_sensitive_words_error(retry_exc):
                        raise
                    task.fallback_stage = "local_summary"
                    task.notices.append("精简上下文仍被模型服务商拦截，已生成基于来源列表的保守总结。")
                    logger.warning(
                        "Compact context retry was also filtered for task %s (%s); using local fallback summary",
                        task.id,
                        task.title,
                    )
                    summary_text = self._build_provider_fallback_summary(task, sources_summary)
                else:
                    task.fallback_stage = "compact_retry_success"
                    logger.info(
                        "Compact context retry succeeded for task %s (%s)",
                        task.id,
                        task.title,
                    )
            # END provider-compat
            self._drain_tool_events(state)

        task.summary = summary_text.strip() if summary_text else "暂无可用信息"
        task.status = "completed"
        self._score_task_confidence(task)

        if emit_stream:
            for event in self._drain_tool_events(state, step=step):
                yield event
            yield {
                "type": "task_status",
                "task_id": task.id,
                "status": "completed",
                "summary": task.summary,
                "sources_summary": task.sources_summary,
                "note_id": task.note_id,
                "note_path": task.note_path,
                "query_variants": task.query_variants,
                "search_result_count": task.search_result_count,
                "confidence_score": task.confidence_score,
                "confidence_label": task.confidence_label,
                "confidence_reason": task.confidence_reason,
                "fallback_used": task.fallback_used,
                "fallback_reason": task.fallback_reason,
                "fallback_stage": task.fallback_stage,
                "step": step,
            }
        else:
            self._drain_tool_events(state)

    def _stream_task_summary(
        self,
        state: SummaryState,
        task: TodoItem,
        context: str,
        *,
        step: int | None,
    ) -> Generator[dict[str, Any], None, str]:
        """Stream a task summary and return its collected final text."""

        summary_stream, summary_getter = self.summarizer.stream_task_summary(state, task, context)
        try:
            for event in self._drain_tool_events(state, step=step):
                yield event
            for chunk in summary_stream:
                if chunk:
                    yield {
                        "type": "task_summary_chunk",
                        "task_id": task.id,
                        "content": chunk,
                        "note_id": task.note_id,
                        "step": step,
                    }
                for event in self._drain_tool_events(state, step=step):
                    yield event
        finally:
            summary_text = summary_getter()

        return summary_text

    # BEGIN provider-compat: last-resort local summary when provider filters all context
    @staticmethod
    def _build_provider_fallback_summary(task: TodoItem, sources_summary: str | None) -> str:
        """Return an honest local fallback when the upstream model refuses the context."""

        source_text = sources_summary.strip() if sources_summary else "暂无可用来源"
        return (
            "## 任务总结\n\n"
            "模型服务商拒绝了该任务的检索上下文，系统已自动尝试精简来源后重试，但仍被拦截。"
            "因此这里不编造具体结论，只保留任务目标和可追溯来源，建议换用更宽松的模型服务或关闭全文抓取后重新运行。\n\n"
            f"- 任务名称：{task.title}\n"
            f"- 任务目标：{task.intent}\n"
            f"- 检索查询：{task.query}\n\n"
            "### 可用来源\n"
            f"{source_text}"
        )
    # END provider-compat

    # BEGIN retrieval-confidence: heuristic confidence score from observable signals
    @staticmethod
    def _score_task_confidence(task: TodoItem) -> None:
        """Score confidence using retrieval coverage and execution quality signals."""

        source_count = max(0, task.search_result_count)
        query_count = len(task.query_variants or [])

        if task.status in {"failed", "skipped"}:
            task.confidence_score = 0.2 if source_count else 0.1
            task.confidence_label = "低"
            task.confidence_reason = "任务未正常完成，结论可靠性较低。"
            return

        score = 0.35
        reasons: list[str] = []

        if source_count >= 8:
            score += 0.35
            reasons.append("去重来源数量充足")
        elif source_count >= 5:
            score += 0.25
            reasons.append("去重来源数量较充分")
        elif source_count >= 3:
            score += 0.15
            reasons.append("有一定来源支撑")
        elif source_count > 0:
            score += 0.05
            reasons.append("来源数量偏少")
        else:
            score -= 0.2
            reasons.append("未检索到有效来源")

        if query_count >= 3:
            score += 0.15
            reasons.append("已启用多检索覆盖多个查询角度")
        elif query_count == 2:
            score += 0.08
            reasons.append("已使用两个查询角度")
        else:
            reasons.append("仅使用单一查询")

        if task.summary and task.summary.strip() and task.summary.strip() != "暂无可用信息":
            score += 0.1
            reasons.append("任务总结已生成")
        else:
            score -= 0.15
            reasons.append("任务总结信息不足")

        if task.fallback_stage == "compact_retry_success":
            score -= 0.1
            reasons.append("模型服务曾拦截原始上下文，精简上下文重试成功")
        elif task.fallback_stage == "local_summary":
            score -= 0.22
            reasons.append("精简上下文仍被拦截，最终使用本地保守总结")
        elif task.fallback_used:
            score -= 0.15
            reasons.append("模型服务曾触发智能降级")

        if task.fallback_stage == "local_summary":
            score = min(score, 0.55)
        score = min(0.95, max(0.05, score))
        task.confidence_score = round(score, 2)
        if score >= 0.8:
            task.confidence_label = "高"
        elif score >= 0.55:
            task.confidence_label = "中"
        else:
            task.confidence_label = "低"
        task.confidence_reason = "；".join(reasons) + "。"
    # END retrieval-confidence

    def _drain_tool_events(
        self,
        state: SummaryState,
        *,
        step: int | None = None,
    ) -> list[dict[str, Any]]:
        """Proxy to the shared tool call tracker."""
        events = self._tool_tracker.drain(state, step=step)
        if self._tool_event_sink_enabled:
            return []
        return events

    @property
    def _tool_call_events(self) -> list[dict[str, Any]]:
        """Expose recorded tool events for legacy integrations."""
        return self._tool_tracker.as_dicts()

    def _serialize_task(self, task: TodoItem) -> dict[str, Any]:
        """Convert task dataclass to serializable dict for frontend."""
        return {
            "id": task.id,
            "title": task.title,
            "intent": task.intent,
            "query": task.query,
            "status": task.status,
            "summary": task.summary,
            "sources_summary": task.sources_summary,
            "note_id": task.note_id,
            "note_path": task.note_path,
            "stream_token": task.stream_token,
            "fallback_used": task.fallback_used,
            "fallback_reason": task.fallback_reason,
            "fallback_stage": task.fallback_stage,
            "query_variants": task.query_variants,
            "search_result_count": task.search_result_count,
            "confidence_score": task.confidence_score,
            "confidence_label": task.confidence_label,
            "confidence_reason": task.confidence_reason,
        }

    def _persist_final_report(self, state: SummaryState, report: str) -> dict[str, Any] | None:
        if not self.note_tool or not report or not report.strip():
            return None

        note_title = f"研究报告：{state.research_topic}".strip() or "研究报告"
        tags = ["deep_research", "report"]
        content = report.strip()

        note_id = self._find_existing_report_note_id(state)
        response = ""

        if note_id:
            response = self.note_tool.run(
                {
                    "action": "update",
                    "note_id": note_id,
                    "title": note_title,
                    "note_type": "conclusion",
                    "tags": tags,
                    "content": content,
                }
            )
            if response.startswith("❌"):
                note_id = None

        if not note_id:
            response = self.note_tool.run(
                {
                    "action": "create",
                    "title": note_title,
                    "note_type": "conclusion",
                    "tags": tags,
                    "content": content,
                }
            )
            note_id = self._extract_note_id_from_text(response)

        if not note_id:
            return None

        state.report_note_id = note_id
        if self.config.notes_workspace:
            note_path = Path(self.config.notes_workspace) / f"{note_id}.md"
            state.report_note_path = str(note_path)
        else:
            note_path = None

        payload = {
            "type": "report_note",
            "note_id": note_id,
            "title": note_title,
            "content": content,
        }
        if note_path:
            payload["note_path"] = str(note_path)

        return payload

    def _find_existing_report_note_id(self, state: SummaryState) -> str | None:
        if state.report_note_id:
            return state.report_note_id

        for event in reversed(self._tool_tracker.as_dicts()):
            if event.get("tool") != "note":
                continue

            parameters = event.get("parsed_parameters") or {}
            if not isinstance(parameters, dict):
                continue

            action = parameters.get("action")
            if action not in {"create", "update"}:
                continue

            note_type = parameters.get("note_type")
            if note_type != "conclusion":
                title = parameters.get("title")
                if not (isinstance(title, str) and title.startswith("研究报告")):
                    continue

            note_id = parameters.get("note_id")
            if not note_id:
                note_id = self._tool_tracker._extract_note_id(event.get("result", ""))  # type: ignore[attr-defined]

            if note_id:
                return note_id

        return None

    @staticmethod
    def _extract_note_id_from_text(response: str) -> str | None:
        if not response:
            return None

        match = re.search(r"ID:\s*([^\n]+)", response)
        if not match:
            return None

        return match.group(1).strip()


def run_deep_research(topic: str, config: Configuration | None = None) -> SummaryStateOutput:
    """Convenience function mirroring the class-based API."""
    agent = DeepResearchAgent(config=config)
    return agent.run(topic)
