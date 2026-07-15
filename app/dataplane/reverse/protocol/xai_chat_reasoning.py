"""Reasoning normalization and aggregation for XAI app-chat streams."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_GENERIC_HEADERS = {
    "",
    "thinking about your request",
}

_PROGRESSIVE_HINTS = (
    "正在", "准备", "计划", "查找", "搜索", "浏览", "确认", "核对", "整合", "挖掘", "比对",
    "checking", "browsing", "verifying", "integrating", "digging", "cross-checking", "searching", "planning",
)

_FINDING_HINTS = (
    "尚未", "已经", "已", "确认", "表明", "说明", "显示", "主要", "通常", "支持", "出现", "启动",
    "持续", "提升", "更新", "灰度", "发布", "上线", "多模态", "视觉", "专家", "context", "token",
    "参数", "每天", "大潮", "小潮", "半日潮", "引力", "周期", "模式", "confirmed", "launched",
    "released", "rollout", "testing", "native multimodal", "widely believed", "latest",
)

_LOW_VALUE_PREFIXES = (
    "用户", "user", "i can", "我可以", "我收集", "建议", "need", "需要", "应该", "since instructions",
    "proposed", "mermaid", "可以用", "我建议",
)

_TRACK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("latest_updates", ("最新", "latest", "today", "recent", "最近", "update", "news", "本周", "4月", "april")),
    ("release_status", ("release date", "released", "release", "launch", "上线", "发布", "正式发布", "current status")),
    ("gray_rollout", ("灰度", "grayscale", "gray release", "灰度测试", "内测", "rollout")),
    ("official_confirmation", ("official", "官网", "official site", "site:", "platform.deepseek.com", "deepseek.ai")),
    ("ui_modes", ("vision", "视觉", "expert", "专家模式", "fast", "default", "ui", "界面", "mode")),
    ("v4_lite", ("v4 lite", "sealion", "sealion-lite", "海狮")),
    ("specs_architecture", ("specs", "parameters", "architecture", "engram", "mhc", "moe", "context", "benchmarks", "规格", "参数", "架构", "万亿")),
    ("definition_basics", ("定义", "解释", "什么是", "what is", "phenomenon", "现象")),
    ("causes_mechanism", ("成因", "原因", "cause", "causes", "gravity", "引力", "机制")),
    ("categories_types", ("春潮", "小潮", "半日潮", "全日潮", "类型", "分类")),
    ("impacts_applications", ("影响", "应用", "发电", "航运", "生活", "生态")),
)

_ZH_LABELS = {
    "understanding": "理解问题",
    "scope": "检索范围",
    "evidence": "核验与证据",
    "finding": "关键发现",
    "latest_updates": "最新动态",
    "release_status": "发布状态与上线节奏",
    "gray_rollout": "灰度进展",
    "official_confirmation": "官方渠道确认",
    "ui_modes": "Expert / Vision 模式关联",
    "v4_lite": "V4 Lite 与 Sealion 线索",
    "specs_architecture": "规格、架构与上下文能力",
    "definition_basics": "定义与基础解释",
    "causes_mechanism": "成因与机制",
    "categories_types": "分类与相关类型",
    "impacts_applications": "影响与应用",
}

_EN_LABELS = {
    "understanding": "Understanding",
    "scope": "Research Scope",
    "evidence": "Verification",
    "finding": "Key Findings",
    "latest_updates": "latest updates",
    "release_status": "release status and rollout timing",
    "gray_rollout": "gray rollout progress",
    "official_confirmation": "official confirmation",
    "ui_modes": "Expert / Vision mode signals",
    "v4_lite": "V4 Lite and Sealion clues",
    "specs_architecture": "specs, architecture, and context capability",
    "definition_basics": "definition and basic explanation",
    "causes_mechanism": "causes and mechanism",
    "categories_types": "categories and related types",
    "impacts_applications": "impacts and applications",
}


@dataclass(slots=True)
class ReasoningEvent:
    section: str
    text: str
    track: str = ""
    evidence_level: int = 0
    dedupe_key: str = ""


class ReasoningAggregator:
    """Normalize raw stream fragments into enterprise-style reasoning output."""

    __slots__ = (
        "_language",
        "_en_votes",
        "_zh_votes",
        "_agent_search_started",
        "_emitted_keys",
        "_seen_tracks",
        "_seen_findings",
        "_pending_events",
        "_section_started",
        "_track_best_level",
        "_track_emit_counts",
    )

    def __init__(self) -> None:
        self._language: str | None = None
        self._en_votes = 0
        self._zh_votes = 0
        self._agent_search_started = False
        self._emitted_keys: set[str] = set()
        self._seen_tracks: set[str] = set()
        self._seen_findings: set[str] = set()
        self._pending_events: list[ReasoningEvent] = []
        self._section_started: set[str] = set()
        self._track_best_level: dict[tuple[str, str], int] = {}
        self._track_emit_counts: dict[tuple[str, str], int] = {}

    def on_thinking(
        self,
        token: str,
        *,
        tag: str | None,
        rollout: str | None,
        step_id: int | None,
    ) -> list[str]:
        self._observe_language(token)
        tag_name = str(tag or "").strip()
        text = str(token or "").strip()
        if not text:
            return []

        if tag_name == "header":
            event = self._normalize_header(text, step_id=step_id)
            return self._dispatch(event) if event else []

        if tag_name == "summary":
            event = self._normalize_summary(text, step_id=step_id)
            return self._dispatch(event) if event else []

        event = self._normalize_summary(text, step_id=step_id)
        return self._dispatch(event) if event else []

    def on_tool_usage(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        rollout: str | None,
        step_id: int | None,
    ) -> list[str]:
        lines: list[str] = []
        self._observe_language(str(args.get("query") or args.get("message") or args.get("instructions") or ""))

        if tool_name == "web_search":
            query = str(args.get("query") or args.get("q") or "").strip()
            if not query:
                return []
            if str(rollout or "").startswith("Agent") and not self._agent_search_started:
                self._agent_search_started = True
                lines.extend(self._dispatch(ReasoningEvent(
                    "scope",
                    self._localized_line("agents_started"),
                    dedupe_key="scope:agents_started",
                )))
            track = self._infer_track(query)
            if not track:
                return lines
            lines.extend(self._dispatch(ReasoningEvent(
                "scope",
                self._localized_track_line(track),
                track=track,
                evidence_level=1,
                dedupe_key=f"scope:web:{track}",
            )))
            return lines

        if tool_name in {"x_search", "x_keyword_search", "x_semantic_search"}:
            query = str(args.get("query") or "").strip()
            track = self._infer_track(query)
            if not track:
                return []
            return self._dispatch(ReasoningEvent(
                "evidence",
                self._localized_social_line(track),
                track=track,
                evidence_level=2,
                dedupe_key=f"evidence:social:{track}",
            ))

        if tool_name == "browse_page":
            url = str(args.get("url") or "").strip()
            source_kind, track = self._classify_page_source(url, args)
            if not source_kind:
                return []
            return self._dispatch(ReasoningEvent(
                "evidence",
                self._localized_browse_line(source_kind, track),
                track=track or source_kind,
                evidence_level=4 if source_kind in {"official", "product"} else 3,
                dedupe_key=f"evidence:browse:{source_kind}:{track or ''}",
            ))

        if tool_name in {"search_images", "image_search"}:
            description = str(args.get("image_description") or args.get("imageDescription") or "").strip()
            if not description:
                return []
            topic = self._classify_image_topic(description)
            if not topic:
                return []
            return self._dispatch(ReasoningEvent(
                "scope",
                self._localized_image_line(topic),
                track="visual_assets",
                evidence_level=1,
                dedupe_key=f"scope:image:{topic}",
            ))

        if tool_name == "chatroom_send":
            message = str(args.get("message") or "").strip()
            if not message:
                return []
            lines = []
            for section, text, track, level in self._extract_report_events(message):
                lines.extend(self._dispatch(ReasoningEvent(
                    section,
                    text,
                    track=track,
                    evidence_level=level,
                    dedupe_key=f"{section}:report:{track}:{self._normalize_key(text)}",
                )))
            return lines

        if tool_name == "code_execution":
            return self._dispatch(ReasoningEvent(
                "evidence",
                self._localized_line("code_execution"),
                dedupe_key="evidence:code_execution",
            ))

        return []

    def finalize(self) -> list[str]:
        if not self._pending_events:
            return []
        if self._language is None:
            self._language = "en" if self._en_votes > 0 and self._zh_votes == 0 else "zh"
        return self._flush_pending()

    def _normalize_header(self, text: str, *, step_id: int | None) -> ReasoningEvent | None:
        stripped = text.strip()
        if stripped.lower() in _GENERIC_HEADERS:
            return None
        section = "understanding" if not self._looks_like_verification(stripped) and (step_id or 0) <= 1 else "evidence"
        return ReasoningEvent(section, self._to_bullet_text(stripped), dedupe_key=f"{section}:header:{self._normalize_key(stripped)}")

    def _normalize_summary(self, text: str, *, step_id: int | None) -> ReasoningEvent | None:
        summary = text.lstrip("- ").strip()
        if not summary:
            return None
        if summary.startswith(("建议搜索", "正在调用工具搜索")):
            return None

        track = self._infer_track(summary)
        if self._looks_like_progress(summary):
            section = "evidence" if self._looks_like_verification(summary) else "scope"
            return ReasoningEvent(section, self._to_bullet_text(summary), track=track, evidence_level=2 if section == "evidence" else 1, dedupe_key=f"{section}:summary:{self._normalize_key(summary)}")

        if self._looks_like_finding(summary):
            if self._is_unconfirmed_signal(summary):
                return ReasoningEvent("evidence", self._to_bullet_text(summary), track=track, evidence_level=2, dedupe_key=f"evidence:summary:{self._normalize_key(summary)}")
            if not self._agent_search_started and (step_id or 0) <= 1:
                return ReasoningEvent("understanding", self._to_bullet_text(summary), track=track, evidence_level=2, dedupe_key=f"understanding:summary:{self._normalize_key(summary)}")
            return ReasoningEvent("finding", self._to_bullet_text(summary), track=track, evidence_level=3, dedupe_key=f"finding:summary:{self._normalize_key(summary)}")

        section = "understanding" if (step_id or 0) <= 1 else "scope"
        return ReasoningEvent(section, self._to_bullet_text(summary), track=track, evidence_level=1, dedupe_key=f"{section}:summary:{self._normalize_key(summary)}")

    def _extract_report_events(self, message: str) -> list[tuple[str, str, str, int]]:
        parts = re.split(r"(?:\n+|[。！？!?；;]+|\s+-\s+)", message.replace("\\n", "\n"))
        candidates: list[tuple[int, str]] = []
        for raw_part in parts:
            clause = self._clean_report_clause(raw_part)
            if not clause:
                continue
            if self._language == "zh" and not re.search(r"[\u4e00-\u9fff]", clause):
                continue
            if self._language == "en" and re.search(r"[\u4e00-\u9fff]", clause):
                continue
            score = self._score_report_clause(clause)
            if score <= 0:
                continue
            candidates.append((score, clause))

        candidates.sort(key=lambda item: (-item[0], len(item[1])))
        results: list[tuple[str, str, str, int]] = []
        seen_local: set[str] = set()
        seen_track_counts: dict[tuple[str, str], int] = {}
        for _, clause in candidates:
            key = self._normalize_key(clause)
            if key in seen_local:
                continue
            seen_local.add(key)
            track = self._infer_track(clause)
            section = "finding" if self._looks_like_finding(clause) else "evidence"
            if self._is_unconfirmed_signal(clause):
                section = "evidence"
            track_key = (section, track or "_")
            current_track_count = seen_track_counts.get(track_key, 0)
            max_track_count = 2 if section == "finding" else 1
            if current_track_count >= max_track_count:
                continue
            seen_track_counts[track_key] = current_track_count + 1
            level = self._infer_evidence_level(clause, default=3 if section == "finding" else 2)
            results.append((section, self._to_bullet_text(clause), track, level))
            if len(results) >= 6:
                break
        results.sort(key=lambda item: (0 if item[0] == "evidence" else 1, item[2], -item[3]))
        return results

    def _dispatch(self, event: ReasoningEvent) -> list[str]:
        if self._language is None:
            self._pending_events.append(event)
            if self._zh_votes > 0:
                self._language = "zh"
            elif self._en_votes >= 3:
                self._language = "en"
            elif len(self._pending_events) < 4:
                return []
            else:
                self._language = "en"
            return self._flush_pending()

        lines: list[str] = []
        if self._pending_events:
            lines.extend(self._flush_pending())
        lines.extend(self._emit(event))
        return lines

    def _flush_pending(self) -> list[str]:
        lines: list[str] = []
        pending = self._pending_events
        self._pending_events = []
        for event in pending:
            lines.extend(self._emit(event))
        return lines

    def _emit(self, event: ReasoningEvent) -> list[str]:
        text = event.text.strip()
        if not text:
            return []
        if event.section == "scope" and ("evidence" in self._section_started or "finding" in self._section_started):
            return []
        if event.section == "evidence" and "finding" in self._section_started:
            if event.evidence_level >= 4 or event.track in {
                "latest_updates",
                "release_status",
                "official_confirmation",
                "specs_architecture",
                "v4_lite",
            }:
                promoted_key = event.dedupe_key or f"evidence:{self._normalize_key(text)}"
                event = ReasoningEvent(
                    "finding",
                    text,
                    track=event.track,
                    evidence_level=event.evidence_level,
                    dedupe_key=f"finding:promoted:{promoted_key}",
                )
            else:
                return []

        dedupe_key = event.dedupe_key or f"{event.section}:{self._normalize_key(text)}"
        if dedupe_key in self._emitted_keys:
            return []

        if event.track:
            count_key = (event.section, event.track)
            emitted_count = self._track_emit_counts.get(count_key, 0)
            max_per_track = 1 if event.section in {"scope", "evidence"} else 2
            if emitted_count >= max_per_track and not dedupe_key.endswith("agents_started"):
                return []
            best_key = (event.section, event.track)
            best_level = self._track_best_level.get(best_key, -1)
            if best_level > event.evidence_level:
                return []
            self._track_best_level[best_key] = max(best_level, event.evidence_level)
            self._track_emit_counts[count_key] = emitted_count + 1

        self._emitted_keys.add(dedupe_key)
        lines: list[str] = []
        if event.section not in self._section_started:
            self._section_started.add(event.section)
            lines.append(self._section_title(event.section) + "\n")
        lines.append(text + "\n")
        return lines

    def _observe_language(self, text: str) -> None:
        if not text:
            return
        cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
        en_count = len(re.findall(r"[A-Za-z]", text))
        if cjk_count >= 4 or cjk_count > max(2, en_count // 2):
            self._zh_votes += 1
            if self._language is None:
                self._language = "zh"
            return
        if en_count >= 4:
            self._en_votes += 1

    def _section_title(self, section: str) -> str:
        labels = _ZH_LABELS if self._language != "en" else _EN_LABELS
        return labels.get(section, section)

    def _localized_line(self, key: str) -> str:
        zh_map = {
            "agents_started": "- 已启动并行代理进行交叉检索与核验。",
            "code_execution": "- 正在执行代码或生成可运行内容。",
        }
        en_map = {
            "agents_started": "- Parallel agents have started cross-checking the topic.",
            "code_execution": "- Executing code or generating runnable content.",
        }
        mapping = zh_map if self._language != "en" else en_map
        return mapping[key]

    def _localized_track_line(self, track: str) -> str:
        label = self._track_label(track)
        if self._language == "en":
            return f"- Parallel research: {label}."
        return f"- 并行检索：{label}。"

    def _localized_social_line(self, track: str) -> str:
        label = self._track_label(track)
        if self._language == "en":
            return f"- Social cross-check: {label}."
        return f"- 社媒交叉核验：{label}。"

    def _localized_browse_line(self, source_kind: str, track: str) -> str:
        track_label = self._track_label(track) if track else ""
        if self._language == "en":
            mapping = {
                "official": "Page verification: official site and official pages",
                "product": "Page verification: product page and live UI",
                "community": "Page verification: public reports and community write-ups",
            }
        else:
            mapping = {
                "official": "页面核对：官网与官方页面",
                "product": "页面核对：产品页面与实际界面",
                "community": "页面核对：公开报道与社区文章",
            }
        base = mapping[source_kind]
        if track_label:
            connector = ", focusing on " if self._language == "en" else "，重点核对"
            return f"- {base}{connector}{track_label}{'.' if self._language == 'en' else '。'}"
        return f"- {base}{'.' if self._language == 'en' else '。'}"

    def _localized_image_line(self, topic: str) -> str:
        if self._language == "en":
            mapping = {
                "diagram": "- Visual asset search: diagrams and explanatory graphics.",
                "photo": "- Visual asset search: real-world comparison photos.",
                "generic": "- Visual asset search: supporting image references.",
            }
        else:
            mapping = {
                "diagram": "- 视觉素材检索：示意图与结构说明素材。",
                "photo": "- 视觉素材检索：实景对比图片。",
                "generic": "- 视觉素材检索：补充说明图片。",
            }
        return mapping[topic]

    def _track_label(self, track: str) -> str:
        labels = _ZH_LABELS if self._language != "en" else _EN_LABELS
        return labels.get(track, track)

    def _infer_track(self, text: str) -> str:
        lowered = self._compact_query(text).lower()
        if not lowered:
            return ""
        for track, keywords in _TRACK_RULES:
            if any(keyword in lowered for keyword in keywords):
                return track
        return ""

    def _classify_page_source(self, url: str, args: dict[str, Any]) -> tuple[str, str]:
        lowered = url.lower()
        instructions = str(args.get("instructions") or "")
        track = self._pick_browse_track(f"{url} {instructions}")
        if any(domain in lowered for domain in ("deepseek.ai", "deepseek.com")):
            if "chat.deepseek.com" in lowered or "platform.deepseek.com" in lowered:
                return "product", track or "ui_modes"
            return "official", track or "official_confirmation"
        if url:
            return "community", track
        return "", track

    def _pick_browse_track(self, text: str) -> str:
        lowered = self._compact_query(text).lower()
        priority = (
            ("ui_modes", ("expert", "vision", "mode", "界面", "ui")),
            ("release_status", ("release", "released", "launch", "发布", "上线", "status")),
            ("specs_architecture", ("spec", "parameter", "architecture", "context", "engram", "moe", "规格", "参数", "架构", "上下文")),
            ("v4_lite", ("v4 lite", "sealion", "sealion-lite", "海狮")),
            ("official_confirmation", ("official", "官网", "current models", "offering")),
        )
        for track, keywords in priority:
            if any(keyword in lowered for keyword in keywords):
                return track
        return self._infer_track(text)

    def _classify_image_topic(self, text: str) -> str:
        lowered = text.lower()
        if any(token in lowered for token in ("diagram", "示意图", "bulge")):
            return "diagram"
        if any(token in lowered for token in ("photo", "照片", "real", "high tide", "low tide", "高潮", "低潮")):
            return "photo"
        return "generic"

    def _looks_like_progress(self, text: str) -> bool:
        lowered = text.lower()
        return any(hint in lowered for hint in _PROGRESSIVE_HINTS)

    def _looks_like_verification(self, text: str) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in ("确认", "核对", "浏览", "整合", "比对", "check", "verify", "browse", "integrat"))

    def _looks_like_finding(self, text: str) -> bool:
        lowered = text.lower()
        if self._looks_like_progress(text):
            return False
        return any(hint in lowered for hint in _FINDING_HINTS)

    def _clean_report_clause(self, raw_part: str) -> str:
        clause = re.sub(r"\s+", " ", raw_part).strip(" -•\t")
        if not clause:
            return ""
        delimiter = "：" if "：" in clause else ":" if ":" in clause else ""
        if delimiter:
            head, tail = clause.split(delimiter, 1)
            head_lower = head.strip().lower()
            if len(head.strip()) <= 18 or any(token in head_lower for token in ("总结", "最新", "关键", "补充", "latest", "summary", "note")):
                clause = tail.strip()
        clause = clause.strip(" -•\t")
        clause = re.sub(r"^(?:我知道|我收集了可靠信息|我收集到的?信息|从搜索结果总结|详细解释要点(?:（[^）]+）)?|补充)\s*", "", clause)
        clause = re.sub(r"^(?:that|it shows|it seems)\s+", "", clause, flags=re.IGNORECASE)
        if len(clause) < 8:
            return ""
        lowered = clause.lower()
        if any(lowered.startswith(prefix) for prefix in _LOW_VALUE_PREFIXES):
            return ""
        if "?" in clause or "？" in clause:
            return ""
        return self._compact_text(clause, limit=120)

    def _score_report_clause(self, clause: str) -> int:
        lowered = clause.lower()
        score = 0
        if any(hint in lowered for hint in _FINDING_HINTS):
            score += 3
        if re.search(r"\b\d+(?:\.\d+)?\b", clause):
            score += 2
        if any(token in clause for token in ("月", "日", "年", "小时", "分钟")):
            score += 1
        if any(token in clause for token in ("重要", "航运", "渔业", "发电", "生态", "模式", "视觉")):
            score += 1
        if any(token in lowered for token in ("可能", "rumor", "传闻", "widely believed", "believed")):
            score -= 1
        if any(token in lowered for token in ("可以", "suggest", "建议", "should", "friendly", "reply")):
            score -= 2
        if len(clause) > 150:
            score -= 1
        return score

    def _infer_evidence_level(self, clause: str, *, default: int) -> int:
        lowered = clause.lower()
        if any(token in lowered for token in ("官网", "official", "chat ui", "界面更新", "页面")):
            return 4
        if any(token in lowered for token in ("x平台", "x posts", "社区", "widely believed", "传闻", "rumor")):
            return max(2, default - 1)
        return default

    def _is_unconfirmed_signal(self, clause: str) -> bool:
        lowered = clause.lower()
        return any(
            token in lowered
            for token in (
                "x平台", "x posts", "社区", "community", "widely believed", "believed",
                "传闻", "rumor", "曝光", "泄露",
            )
        )

    def _to_bullet_text(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        stripped = self._ensure_terminal_punctuation(stripped)
        return f"- {stripped}"

    def _ensure_terminal_punctuation(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        if stripped.endswith(("。", "！", "？", ".", "!", "?")):
            return stripped
        if re.search(r"[\u4e00-\u9fff]", stripped):
            return stripped + "。"
        return stripped + "."

    def _compact_query(self, text: str) -> str:
        cleaned = re.sub(r"\b(?:or|and|site:[^\s]+|since:\S+|from:\S+|date:\S+)\b", " ", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"[()\"']", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _compact_text(self, text: str, *, limit: int) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    def _normalize_key(self, text: str) -> str:
        lowered = text.lower()
        lowered = re.sub(r"https?://\S+", "", lowered)
        lowered = re.sub(r"[^\w\u4e00-\u9fff]+", "", lowered)
        return lowered


__all__ = ["ReasoningAggregator", "ReasoningEvent"]
