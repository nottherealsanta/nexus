"""The retrieval eval of MANAGER_PLAN.md section 11: ~50 seeded sessions, ~100 phrasings, offline.

It measures what matters for acting on the user's behalf: how often the top result is right (top-1), how often
the Manager would act on a *confident* wrong or unwarranted answer (false confidence), and whether a genuine
tie is put to the user as a question. It prints a report (`pytest -s tests/test_manager_eval.py`) and fails if a
number regresses past the bounds below, which sit just under the measured values.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from manager_eval_data import PHRASINGS, SESSIONS
from manager_seed import project, seed

from nexus.manager.retrieval import Retriever
from nexus.store import Store

NOW = 1_800_000_000.0


@dataclass
class Report:
    label: str
    total: int = 0
    targeted: int = 0
    top1: int = 0
    confident_correct: int = 0
    asked: int = 0
    should_ask: int = 0
    none_total: int = 0
    none_clean: int = 0
    false_confident: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    by_kind: dict[str, list[int]] = field(default_factory=dict)

    def rate(self, n: int, d: int) -> float:
        return n / d if d else 0.0

    def line(self) -> str:
        kinds = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(self.by_kind.items()))
        return (f"[{self.label}] {self.total} phrasings | top-1 {self.top1}/{self.targeted} "
                f"({self.rate(self.top1, self.targeted):.0%}) | confident-and-correct {self.confident_correct}/{self.targeted} "
                f"| ties asked {self.asked}/{self.should_ask} | none clean {self.none_clean}/{self.none_total} | "
                f"false confidence {len(self.false_confident)}/{self.total} "
                f"({self.rate(len(self.false_confident), self.total):.1%}) | top-1 by kind: {kinds}")


def build(tmp_path, *, topics: bool) -> tuple[Store, Retriever, dict[str, str]]:
    store = Store(tmp_path / ("topics.db" if topics else "plain.db"))
    projects: dict[str, object] = {}
    keys: dict[str, str] = {}
    for key, proj, title, user, answer, topic, age in SESSIONS:
        projects.setdefault(proj, project(store, tmp_path / "proj", proj))
        session = seed(store, projects[proj], title=title, user=user, answer=answer,
                       topic=topic if topics else None, now=NOW, age_days=age)  # type: ignore[arg-type]
        keys[session.id] = key
    return store, Retriever(store, clock=lambda: NOW), keys


def evaluate(label: str, retriever: Retriever, keys: dict[str, str]) -> Report:
    report = Report(label)
    for query, expected, kind in PHRASINGS:
        result = retriever.find_sessions(query, limit=8)
        best = keys.get(result.best.session_id) if result.best else None
        choice = keys.get(result.choice.session_id) if result.choice else None
        report.total += 1
        if expected not in ("ask", "none"):
            report.targeted += 1
            hit = best == expected
            report.top1 += hit
            report.confident_correct += choice == expected
            row = report.by_kind.setdefault(kind, [0, 0])
            row[0] += hit
            row[1] += 1
            if not hit:
                report.misses.append(f"{query!r}: wanted {expected}, top was {best}")
            if choice is not None and choice != expected:
                report.false_confident.append(f"{query!r}: confidently {choice}, wanted {expected}")
        elif expected == "ask":
            report.should_ask += 1
            if result.results and choice is None:
                report.asked += 1
            elif choice is not None:
                report.false_confident.append(f"{query!r}: confidently {choice}, wanted a question")
            else:
                report.misses.append(f"{query!r}: found nothing, wanted a question")
        else:
            report.none_total += 1
            if choice is None:
                report.none_clean += 1
            else:
                report.false_confident.append(f"{query!r}: confidently {choice}, wanted no match")
    return report


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    out = {}
    for topics in (False, True):
        tmp = tmp_path_factory.mktemp("eval")
        store, retriever, keys = build(tmp, topics=topics)
        out[topics] = evaluate("with topic lines" if topics else "titles, questions, answers", retriever, keys)
        store.close()
    return out


def test_the_eval_reports_honest_numbers(reports, capsys):
    with capsys.disabled():
        print()
        for report in reports.values():
            print(report.line())
            for miss in report.misses[:40]:
                print("   miss:", miss)
            for wrong in report.false_confident[:40]:
                print("   FALSE CONFIDENCE:", wrong)
    assert reports[False].total >= 100 and len(SESSIONS) == 50


def test_top_one_and_false_confidence_stay_within_bounds(reports):
    plain, with_topics = reports[False], reports[True]
    assert plain.rate(plain.top1, plain.targeted) >= 0.88
    assert with_topics.rate(with_topics.top1, with_topics.targeted) >= 0.95
    assert with_topics.top1 >= plain.top1  # topic lines catch paraphrase; they must never make it worse
    assert plain.rate(len(plain.false_confident), plain.total) <= 0.05
    assert with_topics.rate(len(with_topics.false_confident), with_topics.total) <= 0.02
    assert plain.none_clean == plain.none_total and with_topics.none_clean == with_topics.none_total


def test_a_tie_is_always_a_question_never_a_choice(reports):
    for report in reports.values():
        assert report.asked >= report.should_ask - 2  # the misses are listed in the report above
