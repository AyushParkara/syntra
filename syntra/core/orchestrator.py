"""Multi-agent orchestrator: a parallel feature-development pipeline.

Coordinates parallel sub-agents for complex tasks. Each agent has a specific
role (explorer, architect, executor, reviewer) and runs independently.
Results are collected and synthesized by the orchestrator.

This is Syntra's multi-agent workflow engine, built on the planner→executor→
reviewer architecture with parallel fan-out at each phase.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Callable

from .textutil import clip


@dataclass
class AgentTask:
    """A single sub-agent task."""
    id: str
    role: str          # "explorer" | "architect" | "executor" | "reviewer"
    prompt: str
    model_id: str = ""
    status: str = "pending"  # pending | running | done | failed
    result: str = ""
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    tokens_used: int = 0


@dataclass
class OrchestrationPhase:
    """One phase of orchestration (e.g., all explorers, or all reviewers)."""
    name: str
    tasks: list[AgentTask] = field(default_factory=list)
    parallel: bool = True


@dataclass
class OrchestrationPlan:
    """Full multi-agent orchestration plan."""
    goal: str
    phases: list[OrchestrationPhase] = field(default_factory=list)
    status: str = "pending"
    started: float = 0.0
    finished: float = 0.0


class Orchestrator:
    """Runs multi-agent workflows with parallel fanout.

    Usage:
        orch = Orchestrator(call_model=my_call_fn, progress=my_progress_fn)
        plan = orch.plan_feature_dev("Add user authentication")
        result = orch.execute(plan)
    """

    def __init__(
        self,
        *,
        call_model: Callable[[str, str, str], str],  # (role, system, user) -> response
        progress: Callable[[str, dict], None] | None = None,
        max_parallel: int = 3,
    ):
        self._call = call_model
        self._progress = progress or (lambda k, p: None)
        self._max_parallel = max_parallel

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self._progress(kind, payload)
        except Exception:
            pass

    def plan_feature_dev(self, goal: str) -> OrchestrationPlan:
        """Create a feature-development orchestration plan.

        Phases:
        1. Explore — 2 parallel explorers trace different aspects
        2. Architect — 1 architect designs the approach
        3. Execute — sequential execution (uses Syntra's main loop)
        4. Review — 2 parallel reviewers check different dimensions
        """
        plan = OrchestrationPlan(goal=goal)

        plan.phases.append(OrchestrationPhase(
            name="explore",
            tasks=[
                AgentTask(id="exp-1", role="explorer",
                          prompt=f"Trace the codebase to understand how to: {goal}. "
                                 "Focus on architecture, patterns, and existing implementations. "
                                 "List 5-10 key files that are relevant."),
                AgentTask(id="exp-2", role="explorer",
                          prompt=f"Find all code paths, dependencies, and edge cases related to: {goal}. "
                                 "Focus on what could break and what needs to change."),
            ],
            parallel=True,
        ))

        plan.phases.append(OrchestrationPhase(
            name="architect",
            tasks=[
                AgentTask(id="arch-1", role="architect",
                          prompt=f"Based on the exploration results, design a complete implementation plan for: {goal}. "
                                 "Pick ONE approach (not multiple options). Include file changes and order of operations."),
            ],
            parallel=False,
        ))

        plan.phases.append(OrchestrationPhase(
            name="review",
            tasks=[
                AgentTask(id="rev-bugs", role="reviewer",
                          prompt="Review the implementation for bugs and correctness issues. "
                                 "Only report findings with ≥80% confidence. Score each finding."),
                AgentTask(id="rev-quality", role="reviewer",
                          prompt="Review the implementation for code quality, simplicity, and conventions. "
                                 "Focus on DRY violations, unnecessary complexity, and naming."),
            ],
            parallel=True,
        ))

        return plan

    def plan_code_review(self, diff: str, effort: str = "medium") -> OrchestrationPlan:
        """Create a code review orchestration plan."""
        plan = OrchestrationPlan(goal=f"Review code diff (effort: {effort})")

        reviewers = [
            AgentTask(id="rev-bugs", role="reviewer",
                      prompt=f"Review this diff for bugs and correctness. Effort: {effort}. "
                             f"Only report ≥80% confidence findings.\n\n```diff\n{diff[:6000]}\n```"),
        ]
        if effort in ("medium", "high"):
            reviewers.append(
                AgentTask(id="rev-quality", role="reviewer",
                          prompt=f"Review this diff for code quality and simplification. "
                                 f"Find DRY violations, unnecessary complexity.\n\n```diff\n{diff[:6000]}\n```"),
            )
        if effort == "high":
            reviewers.append(
                AgentTask(id="rev-errors", role="reviewer",
                          prompt=f"Review this diff for error handling quality. "
                                 f"Find silent failures, empty catches, missing validation.\n\n```diff\n{diff[:6000]}\n```"),
            )

        plan.phases.append(OrchestrationPhase(
            name="review", tasks=reviewers, parallel=True,
        ))
        return plan

    def execute(self, plan: OrchestrationPlan) -> OrchestrationPlan:
        """Execute an orchestration plan, running phases in order."""
        plan.status = "running"
        plan.started = time.time()
        self._emit("orchestration_start", {"goal": plan.goal, "phases": len(plan.phases)})

        prev_results: list[str] = []

        for phase in plan.phases:
            self._emit("phase", {"phase": phase.name, "tasks": len(phase.tasks)})

            if phase.parallel and len(phase.tasks) > 1:
                self._run_parallel(phase.tasks, prev_results)
            else:
                for task in phase.tasks:
                    self._run_task(task, prev_results)

            # Collect results for next phase
            prev_results.extend(f"[{task.id}] {clip(task.result, 2000)}"
                                for task in phase.tasks if task.status == "done" and task.result)

        plan.status = "done"
        plan.finished = time.time()
        self._emit("orchestration_done", {
            "goal": plan.goal,
            "duration": plan.finished - plan.started,
            "tasks_total": sum(len(p.tasks) for p in plan.phases),
            "tasks_done": sum(1 for p in plan.phases for t in p.tasks if t.status == "done"),
        })
        return plan

    def _run_task(self, task: AgentTask, context: list[str]) -> None:
        """Run a single agent task."""
        task.status = "running"
        task.started = time.time()
        self._emit("agent_start", {"id": task.id, "role": task.role})

        system = self._system_prompt(task.role)
        user_prompt = task.prompt
        if context:
            user_prompt += "\n\nPREVIOUS RESULTS:\n" + "\n\n".join(context[-3:])

        try:
            result = self._call(task.role, system, user_prompt)
            task.result = result
            task.status = "done"
        except Exception as e:
            task.error = str(e)
            task.status = "failed"
        finally:
            task.finished = time.time()
            self._emit("agent_done", {
                "id": task.id, "role": task.role,
                "status": task.status,
                "duration": task.finished - task.started,
            })

    def _run_parallel(self, tasks: list[AgentTask], context: list[str]) -> None:
        """Run multiple tasks in parallel using threads."""
        threads: list[threading.Thread] = []
        for task in tasks:
            t = threading.Thread(
                target=self._run_task,
                args=(task, list(context)),
                name=f"syntra-agent-{task.id}",
                daemon=True,
            )
            threads.append(t)

        # Start in batches of max_parallel
        for i in range(0, len(threads), self._max_parallel):
            batch = threads[i:i + self._max_parallel]
            for t in batch:
                t.start()
            for t in batch:
                t.join(timeout=120)

    def _system_prompt(self, role: str) -> str:
        prompts = {
            "explorer": (
                "You are a code explorer agent. Your job is to trace execution paths, "
                "find relevant files, and understand the codebase structure. "
                "Return a list of key findings and file paths."
            ),
            "architect": (
                "You are a code architect agent. Design a complete implementation plan. "
                "Make confident choices — pick ONE approach, not multiple options. "
                "Include file paths, function signatures, and order of changes."
            ),
            "executor": (
                "You are a code executor agent. Implement the changes described in the plan. "
                "Write clean, tested code. Follow existing patterns in the codebase."
            ),
            "reviewer": (
                "You are a code reviewer agent. Review for bugs, quality, and correctness. "
                "Only report findings with ≥80% confidence. Score each finding 0-100. "
                "Format: FINDING: [confidence] description"
            ),
        }
        return prompts.get(role, prompts["executor"])
