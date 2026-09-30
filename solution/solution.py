"""
Day 14 — AI Evaluation & Benchmarking Pipeline (solution)
AICB-P1: AI Practical Competency Program, Phase 1
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Task 1 — Data Models
# ---------------------------------------------------------------------------

@dataclass
class QAPair:
    """A question-answer pair (one row of the Golden Dataset)."""
    question: str
    expected_answer: str
    context: str = ""
    metadata: dict = field(default_factory=dict)
    retrieved_contexts: list = field(default_factory=list)


@dataclass
class EvalResult:
    """Evaluation result for a single Q&A pair."""
    qa_pair: QAPair
    actual_answer: str
    faithfulness: float
    relevance: float
    completeness: float
    passed: bool
    failure_type: str | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    def overall_score(self) -> float:
        """Mean of faithfulness, relevance, completeness (retrieval metrics excluded)."""
        return (self.faithfulness + self.relevance + self.completeness) / 3.0


# ---------------------------------------------------------------------------
# Task 2 — RAGAS Evaluator (word-overlap heuristics)
# ---------------------------------------------------------------------------

STOPWORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "as", "by", "and", "or",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokenization, ignoring punctuation and stopwords."""
    if not text:
        return set()
    tokens = re.findall(r"\b\w+\b", text.lower())
    return {t for t in tokens if t not in STOPWORDS}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class RAGASEvaluator:
    """Evaluates RAG pipeline outputs using RAGAS-inspired heuristics."""

    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        answer_tokens = _tokenize(answer)
        if not answer_tokens:
            return 1.0
        context_tokens = _tokenize(context)
        return _clamp(len(answer_tokens & context_tokens) / len(answer_tokens))

    def evaluate_relevance(self, answer: str, question: str) -> float:
        question_tokens = _tokenize(question)
        if not question_tokens:
            return 1.0
        answer_tokens = _tokenize(answer)
        return _clamp(len(answer_tokens & question_tokens) / len(question_tokens))

    def evaluate_completeness(self, answer: str, expected: str) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        answer_tokens = _tokenize(answer)
        return _clamp(len(answer_tokens & expected_tokens) / len(expected_tokens))

    # ---- Task 2b — Retrieval-side metrics ---------------------------------

    def evaluate_context_recall(self, contexts: list[str], expected: str) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        union_tokens: set[str] = set()
        for chunk in contexts or []:
            union_tokens |= _tokenize(chunk)
        return _clamp(len(expected_tokens & union_tokens) / len(expected_tokens))

    def evaluate_context_precision(
        self,
        contexts: list[str],
        expected: str,
        relevance_threshold: float = 0.1,
    ) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        # Step 1: mark each chunk (in retriever order) as relevant / not relevant
        relevant_flags: list[bool] = []
        for chunk in contexts:
            coverage = len(_tokenize(chunk) & expected_tokens) / len(expected_tokens)
            relevant_flags.append(coverage >= relevance_threshold)

        total_relevant = sum(relevant_flags)
        if total_relevant == 0:
            return 0.0

        # Steps 2-3: Precision@k at each relevant position, then average
        running_relevant = 0
        precision_sum = 0.0
        for k, is_relevant in enumerate(relevant_flags, start=1):
            if is_relevant:
                running_relevant += 1
                precision_sum += running_relevant / k
        return _clamp(precision_sum / total_relevant)

    def run_full_eval(
        self,
        answer: str,
        question: str,
        context: str,
        expected: str,
        contexts: list[str] | None = None,
    ) -> EvalResult:
        faithfulness = self.evaluate_faithfulness(answer, context)
        relevance = self.evaluate_relevance(answer, question)
        completeness = self.evaluate_completeness(answer, expected)

        passed = faithfulness >= 0.5 and relevance >= 0.5 and completeness >= 0.5

        failure_type: str | None = None
        if not passed:
            if faithfulness < 0.3:
                failure_type = "hallucination"
            elif relevance < 0.3:
                failure_type = "irrelevant"
            elif completeness < 0.3:
                failure_type = "incomplete"
            else:
                failure_type = "off_topic"

        context_recall: float | None = None
        context_precision: float | None = None
        if contexts is not None:
            context_recall = self.evaluate_context_recall(contexts, expected)
            context_precision = self.evaluate_context_precision(contexts, expected)

        return EvalResult(
            qa_pair=QAPair(question=question, expected_answer=expected, context=context),
            actual_answer=answer,
            faithfulness=faithfulness,
            relevance=relevance,
            completeness=completeness,
            passed=passed,
            failure_type=failure_type,
            context_precision=context_precision,
            context_recall=context_recall,
        )


# ---------------------------------------------------------------------------
# Reranking helper (Bonus — Exercise 3.5)
# ---------------------------------------------------------------------------

def rerank_by_overlap(contexts: list[str], query: str) -> list[str]:
    """Sort chunks by word overlap with the query, most-overlapping first."""
    query_tokens = _tokenize(query)
    return sorted(
        contexts,
        key=lambda c: len(_tokenize(c) & query_tokens),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Task 3 — LLM Judge
# ---------------------------------------------------------------------------

class LLMJudge:
    """Uses an LLM to score AI responses according to a rubric."""

    def __init__(self, judge_llm_fn: Callable[[str], str]) -> None:
        self.judge_llm_fn = judge_llm_fn

    def _build_prompt(self, question: str, answer: str, rubric: dict[str, Any]) -> str:
        criteria = "\n".join(f"- {name}: {desc}" for name, desc in rubric.items())
        keys = ", ".join(f'"{name}"' for name in rubric)
        return (
            "You are an impartial judge. Score the answer below on each criterion "
            "from 0.0 to 1.0.\n\n"
            f"Question: {question}\n\n"
            f"Answer: {answer}\n\n"
            f"Rubric:\n{criteria}\n\n"
            f"Respond ONLY with JSON in the form "
            f'{{"scores": {{{keys}: <float>}}, "reasoning": "<short explanation>"}}'
        )

    @staticmethod
    def _to_unit_score(value: Any) -> float:
        """Convert a raw value to [0, 1]. Values > 1 are treated as a 1-5 scale."""
        v = float(value)
        if v > 1.0:
            v = v / 5.0
        return _clamp(v)

    def score_response(
        self,
        question: str,
        answer: str,
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = self._build_prompt(question, answer, rubric)
        raw = self.judge_llm_fn(prompt)
        raw_text = raw if isinstance(raw, str) else str(raw)

        default_scores = {name: 0.5 for name in rubric}
        scores = dict(default_scores)
        reasoning = raw_text

        try:
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            data = json.loads(match.group(0) if match else raw_text)
            if not isinstance(data, dict):
                raise ValueError("JSON is not an object")

            raw_scores = data["scores"] if isinstance(data.get("scores"), dict) else data
            parsed: dict[str, float] = {}
            for name in rubric:
                if name in raw_scores:
                    parsed[name] = self._to_unit_score(raw_scores[name])
            if parsed:
                scores = {name: parsed.get(name, 0.5) for name in rubric}
                if isinstance(data.get("reasoning"), str):
                    reasoning = data["reasoning"]
        except (ValueError, TypeError, KeyError):
            scores = dict(default_scores)

        return {"scores": scores, "reasoning": reasoning}

    def detect_bias(self, scores_batch: list[dict[str, Any]]) -> dict[str, Any]:
        result = {"positional_bias": False, "leniency_bias": False, "severity_bias": False}
        if not scores_batch:
            return result

        # Average score of each item across its criteria
        item_avgs: list[float] = []
        for item in scores_batch:
            values = list((item.get("scores") or {}).values())
            if values:
                item_avgs.append(sum(values) / len(values))
        if not item_avgs:
            return result

        overall_avg = sum(item_avgs) / len(item_avgs)
        result["leniency_bias"] = overall_avg > 0.8
        result["severity_bias"] = overall_avg < 0.3

        # Positional bias: first response scores clearly higher than the rest
        if len(item_avgs) >= 2:
            rest_avg = sum(item_avgs[1:]) / len(item_avgs[1:])
            result["positional_bias"] = item_avgs[0] > rest_avg + 0.1

        return result


# ---------------------------------------------------------------------------
# Task 4 — Benchmark Runner
# ---------------------------------------------------------------------------

def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class BenchmarkRunner:
    """Runs a full evaluation benchmark."""

    def run(
        self,
        qa_pairs: list[QAPair],
        agent_fn: Callable[[str], str],
        evaluator: RAGASEvaluator,
    ) -> list[EvalResult]:
        results: list[EvalResult] = []
        for pair in qa_pairs:
            answer = agent_fn(pair.question)
            contexts = pair.retrieved_contexts if pair.retrieved_contexts else None
            result = evaluator.run_full_eval(
                answer=answer,
                question=pair.question,
                context=pair.context,
                expected=pair.expected_answer,
                contexts=contexts,
            )
            result.qa_pair = pair  # keep the original pair (with metadata)
            results.append(result)
        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        total = len(results)
        passed = sum(1 for r in results if r.passed)

        failure_types: dict[str, int] = {}
        for r in results:
            if r.failure_type:
                failure_types[r.failure_type] = failure_types.get(r.failure_type, 0) + 1

        recalls = [r.context_recall for r in results if r.context_recall is not None]
        precisions = [r.context_precision for r in results if r.context_precision is not None]

        return {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total if total else 0.0,
            "avg_faithfulness": _avg([r.faithfulness for r in results]),
            "avg_relevance": _avg([r.relevance for r in results]),
            "avg_completeness": _avg([r.completeness for r in results]),
            "avg_context_recall": _avg(recalls) if recalls else None,
            "avg_context_precision": _avg(precisions) if precisions else None,
            "failure_types": failure_types,
        }

    def run_regression(self, new_results: list, baseline_results: list) -> dict:
        new_f = _avg([r.faithfulness for r in new_results])
        new_r = _avg([r.relevance for r in new_results])
        new_c = _avg([r.completeness for r in new_results])
        base_f = _avg([r.faithfulness for r in baseline_results])
        base_r = _avg([r.relevance for r in baseline_results])
        base_c = _avg([r.completeness for r in baseline_results])

        eps = 1e-9  # avoid floating-point noise at exactly 0.05
        regressions: list[str] = []
        if base_f - new_f > 0.05 + eps:
            regressions.append("faithfulness")
        if base_r - new_r > 0.05 + eps:
            regressions.append("relevance")
        if base_c - new_c > 0.05 + eps:
            regressions.append("completeness")

        return {
            "new_avg_faithfulness": new_f,
            "new_avg_relevance": new_r,
            "new_avg_completeness": new_c,
            "baseline_avg_faithfulness": base_f,
            "baseline_avg_relevance": base_r,
            "baseline_avg_completeness": base_c,
            "regressions": regressions,
            "passed": len(regressions) == 0,
        }

    def identify_failures(
        self,
        results: list[EvalResult],
        threshold: float = 0.5,
    ) -> list[EvalResult]:
        return [
            r for r in results
            if r.faithfulness < threshold
            or r.relevance < threshold
            or r.completeness < threshold
        ]


# ---------------------------------------------------------------------------
# Task 5 — Failure Analyzer
# ---------------------------------------------------------------------------

ROOT_CAUSE_RETRIEVAL = "Context is missing or irrelevant — improve retrieval"
ROOT_CAUSE_PROMPT = "Answer does not address the question — improve prompt clarity"
ROOT_CAUSE_INCOMPLETE = (
    "Answer is missing key information — increase context window or improve generation"
)
ROOT_CAUSE_MULTIPLE = "Multiple issues detected — review full pipeline"

SUGGESTION_BY_TYPE = {
    "hallucination": "Implement hallucination checker and force answers to cite retrieved context to filter unsupported claims",
    "irrelevant": "Clarify the system prompt and add query rewriting so answers address the user's question directly",
    "incomplete": "Increase chunk size / top-k and add few-shot examples showing complete answers to improve completeness",
    "off_topic": "Improve intent detection and add out-of-scope handling so off-topic answers are redirected",
    "refusal": "Relax over-strict guardrails so the agent answers in-scope questions instead of refusing",
}

GENERIC_SUGGESTIONS = [
    "Add more failing cases to the golden dataset (augment) and re-run the benchmark",
    "Add reranking and hybrid search to improve retrieval quality",
    "Run evaluation in CI/CD as a quality gate and block deploys on regression",
]


class FailureAnalyzer:
    """Analyzes failed evaluation results to identify patterns and suggest fixes."""

    def categorize_failures(self, failures: list[EvalResult]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in failures:
            if f.failure_type:
                counts[f.failure_type] = counts.get(f.failure_type, 0) + 1
        return counts

    def find_root_cause(self, failure: EvalResult) -> str:
        scores = {
            "faithfulness": failure.faithfulness,
            "relevance": failure.relevance,
            "completeness": failure.completeness,
        }
        lowest_value = min(scores.values())
        lowest = [name for name, v in scores.items() if v == lowest_value]

        # Tie for lowest score => no single culprit
        if len(lowest) > 1:
            return ROOT_CAUSE_MULTIPLE
        if lowest[0] == "faithfulness":
            return ROOT_CAUSE_RETRIEVAL
        if lowest[0] == "relevance":
            return ROOT_CAUSE_PROMPT
        return ROOT_CAUSE_INCOMPLETE

    def generate_improvement_log(self, failures: list, suggestions: list[str]) -> str:
        lines = [
            "| Failure ID | Type | Root Cause | Suggested Fix | Status |",
            "|------------|------|------------|---------------|--------|",
        ]
        for i, failure in enumerate(failures):
            failure_id = f"F{i + 1:03d}"
            f_type = failure.failure_type or "unknown"
            root_cause = self.find_root_cause(failure)
            fix = suggestions[i] if i < len(suggestions) else "-"
            row = [failure_id, f_type, root_cause, fix]
            row = [str(c).replace("|", "/") for c in row]
            lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | Open |")
        return "\n".join(lines)

    def generate_improvement_suggestions(self, failures: list[EvalResult]) -> list[str]:
        if not failures:
            return []

        counts = self.categorize_failures(failures)
        # Most frequent failure type first = highest priority
        ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

        suggestions: list[str] = []
        for f_type, _ in ordered:
            if f_type in SUGGESTION_BY_TYPE:
                suggestions.append(SUGGESTION_BY_TYPE[f_type])

        for generic in GENERIC_SUGGESTIONS:
            if len(suggestions) >= 3:
                break
            if generic not in suggestions:
                suggestions.append(generic)
        return suggestions
