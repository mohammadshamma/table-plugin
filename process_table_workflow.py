#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp==2.3.0",
#     "anyio",
# ]
# ///
"""
Deterministic Python workflow runner for table-plugin.
Processes rows of a table using a bounded asyncio worker pool.
Calls table MCP server tools via call_tool (table_job_create, table_job_claim, table_job_submit, table_job_status).
"""

import asyncio
import inspect
import itertools
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_file_var = globals().get("__file__")
PLUGIN_DIR = (
    Path(_file_var).resolve().parent
    if _file_var
    else (Path.home() / ".gemini" / "config" / "plugins" / "table")
)
if PLUGIN_DIR.is_dir() and str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


async def _await_if_needed(val: Any) -> Any:
    if asyncio.iscoroutine(val) or inspect.isawaitable(val):
        return await val
    return val


async def _invoke_tool(
    call_tool_fn: Callable | None, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Helper to invoke an MCP tool via run_workflow's call_tool or server.dispatch seamlessly."""
    if call_tool_fn is None:
        import server
        call_tool_fn = server.dispatch

    # Detect whether call_tool_fn expects (name, args_dict) or run_workflow's call_tool(name, **kwargs)
    use_kwargs = False
    try:
        sig = inspect.signature(call_tool_fn)
        params = list(sig.parameters.values())
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        positional = [
            p
            for p in params
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if has_var_kw and len(positional) <= 1:
            use_kwargs = True
    except Exception:
        pass

    if use_kwargs:
        candidates = [
            name if name.startswith("mcp_") else f"mcp_table_table_{name}",
            name if name.startswith("mcp_") else f"mcp_table_{name}",
            name,
        ]
        last_exc: Exception | None = None
        for cand in candidates:
            try:
                res = await _await_if_needed(call_tool_fn(cand, **args))
                break
            except Exception as exc:
                if getattr(exc, "denied", False):
                    raise
                last_exc = exc
        else:
            for srv in ("table_table", "table"):
                try:
                    res = await _await_if_needed(
                        call_tool_fn(
                            "call_mcp_tool",
                            ServerName=srv,
                            ToolName=name,
                            Arguments=args,
                        )
                    )
                    break
                except Exception as inner_exc:
                    if getattr(inner_exc, "denied", False):
                        raise
                    last_exc = inner_exc
            else:
                return {"error": str(last_exc)}
    else:
        try:
            res = await _await_if_needed(call_tool_fn(name, args))
        except Exception as exc:
            if getattr(exc, "denied", False):
                raise
            return {"error": str(exc)}

    if hasattr(res, "content") and res.content:
        text = res.content[0].text
        return json.loads(text)
    elif isinstance(res, str):
        return json.loads(res)
    elif isinstance(res, dict):
        return res
    return res or {}


async def _invoke_agent(
    agent_fn: Callable,
    prompt: str,
    task: dict[str, Any],
    worker_id: str,
    role: str | None = None,
    model: str | None = None,
    workspace: str = "inherit",
) -> str:
    """Execute single-use agent per row, extracting finish/result."""
    try:
        sig = inspect.signature(agent_fn)
        params = sig.parameters
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        kwargs: dict[str, Any] = {}
        if "schema" in params or has_var_kw:
            kwargs["schema"] = {"type": "string"}
            if ("role" in params or has_var_kw) and role:
                kwargs["role"] = role
            if ("workspace" in params or has_var_kw) and workspace:
                kwargs["workspace"] = workspace
            if ("model" in params or has_var_kw) and model:
                kwargs["model"] = model
        if "task" in params:
            kwargs["task"] = task
        if "worker_id" in params:
            kwargs["worker_id"] = worker_id
        elif "conversation_id" in params:
            kwargs["conversation_id"] = worker_id
    except Exception:
        kwargs = {}

    if kwargs:
        try:
            res = agent_fn(prompt, **kwargs)
        except TypeError:
            res = agent_fn(prompt, {"type": "string"}) if "schema" in kwargs else agent_fn(prompt)
    else:
        res = agent_fn(prompt)

    res = await _await_if_needed(res)

    if isinstance(res, dict):
        if "error" in res and res["error"]:
            raise RuntimeError(str(res["error"]))
        if "finish" in res:
            return str(res["finish"])
        if "result" in res:
            return str(res["result"])
    if res is None:
        return ""
    return str(res)


async def run_table_job_workflow(
    job_table: str | None = None,
    table: str | None = None,
    template: str | None = None,
    prompt: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    workspace: str = "inherit",
    concurrency: int = 5,
    max_attempts: int = 3,
    lease_seconds: float = 600.0,
    max_claims_per_worker: int = 1,
    conversation_id: str | None = None,
    parent_conversation_id: str | None = None,
    call_tool_fn: Callable | None = None,
    agent_fn: Callable | None = None,
    log_fn: Callable | None = None,
    phase_fn: Callable | None = None,
) -> dict[str, Any]:
    """
    Run table processing workflow with bounded asyncio concurrency.
    Supports both pre-created jobs (job_table) and hybrid one-shot (table + template).
    """
    effective_parent_id = (
        conversation_id
        or parent_conversation_id
        or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        or "workflow_parent"
    )

    resolved_template = template or prompt

    # 1. Hybrid one-shot vs pre-created job table flow
    if table and resolved_template:
        resolved_job_table = job_table or f"{table}_job"
        status_res = await _invoke_tool(
            call_tool_fn,
            "table_job_status",
            {"job_table": resolved_job_table, "conversation_id": effective_parent_id},
        )
        if "error" in status_res:
            create_args = {
                "table": table,
                "template": resolved_template,
                "job_table": resolved_job_table,
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
                "max_claims_per_worker": max_claims_per_worker,
                "conversation_id": effective_parent_id,
            }
            create_res = await _invoke_tool(call_tool_fn, "table_job_create", create_args)
            if "error" in create_res:
                raise RuntimeError(f"table_job_create failed: {create_res['error']}")
        elif status_res.get("complete", False):
            return {
                "ok": status_res.get("failed", 0) == 0,
                "job_table": resolved_job_table,
                "total": status_res.get("total", 0),
                "done": status_res.get("done", 0),
                "failed": status_res.get("failed", 0),
                "complete": True,
                "status": status_res,
            }
        job_table = resolved_job_table
    elif job_table:
        status_res = await _invoke_tool(
            call_tool_fn,
            "table_job_status",
            {"job_table": job_table, "conversation_id": effective_parent_id},
        )
        if "error" in status_res:
            raise RuntimeError(f"table_job_status failed for '{job_table}': {status_res['error']}")
        if status_res.get("complete", False):
            return {
                "ok": True,
                "job_table": job_table,
                "total": status_res.get("total", 0),
                "done": status_res.get("done", 0),
                "failed": status_res.get("failed", 0),
                "complete": True,
                "status": status_res,
            }
    else:
        raise ValueError("Either 'job_table' or both 'table' and 'template' (or 'prompt') must be provided")

    if agent_fn is None:
        agent_fn = lambda p: f"Completed: {p}"

    num_workers = max(1, int(concurrency or 5))
    if phase_fn is not None:
        phase_fn(f"Processing {job_table} (concurrency={num_workers})")

    # 2. Worker pool with bounded concurrency
    seq_counter = itertools.count(1)
    work_event = asyncio.Event()

    async def worker_loop():
        while True:
            seq = next(seq_counter)
            worker_id = f"{effective_parent_id}_wf_{seq}"

            claim_res = await _invoke_tool(
                call_tool_fn,
                "table_job_claim",
                {"job_table": job_table, "conversation_id": worker_id},
            )

            if "error" in claim_res:
                await asyncio.sleep(0.05)
                continue

            task = claim_res.get("task")
            if task is not None:
                task_id = task["task_id"]
                row_prompt = task["prompt"]
                if instructions:
                    row_prompt = f"{row_prompt}\n\n{instructions}"
                try:
                    result = await _invoke_agent(
                        agent_fn,
                        row_prompt,
                        task,
                        worker_id,
                        role=f"{job_table} #{task_id}",
                        model=model,
                        workspace=workspace or "inherit",
                    )
                    await _invoke_tool(
                        call_tool_fn,
                        "table_job_submit",
                        {
                            "job_table": job_table,
                            "task_id": task_id,
                            "result": str(result),
                            "conversation_id": worker_id,
                        },
                    )
                except Exception as exc:
                    if log_fn is not None:
                        log_fn(f"Task #{task_id} attempt failed: {exc}")
                    await _invoke_tool(
                        call_tool_fn,
                        "table_job_submit",
                        {
                            "job_table": job_table,
                            "task_id": task_id,
                            "error": str(exc),
                            "conversation_id": worker_id,
                        },
                    )
                finally:
                    work_event.set()
            else:
                status_res = await _invoke_tool(
                    call_tool_fn,
                    "table_job_status",
                    {"job_table": job_table, "conversation_id": effective_parent_id},
                )
                if status_res.get("complete", False):
                    work_event.set()
                    break

                if status_res.get("pending", 0) == 0 and status_res.get("claimed", 0) == 0:
                    work_event.set()
                    break

                work_event.clear()
                try:
                    await asyncio.wait_for(work_event.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass

    worker_tasks = [asyncio.create_task(worker_loop()) for _ in range(num_workers)]
    await asyncio.gather(*worker_tasks)

    final_status = await _invoke_tool(
        call_tool_fn,
        "table_job_status",
        {"job_table": job_table, "conversation_id": effective_parent_id},
    )
    if log_fn is not None:
        log_fn(
            f"Completed '{job_table}': {final_status.get('done', 0)}/{final_status.get('total', 0)} done, "
            f"{final_status.get('failed', 0)} failed."
        )

    return {
        "ok": final_status.get("complete", False) and final_status.get("failed", 0) == 0,
        "job_table": job_table,
        "total": final_status.get("total", 0),
        "done": final_status.get("done", 0),
        "failed": final_status.get("failed", 0),
        "complete": final_status.get("complete", False),
        "status": final_status,
    }


def _build_workflow_coro_from_globals(g: dict[str, Any]):
    """Build coroutine from Antigravity run_workflow injected globals."""
    wf_args = g.get("args") if isinstance(g.get("args"), dict) else {}
    return run_table_job_workflow(
        job_table=wf_args.get("job_table"),
        table=wf_args.get("table"),
        template=wf_args.get("template"),
        prompt=wf_args.get("prompt"),
        instructions=wf_args.get("instructions"),
        model=wf_args.get("model"),
        workspace=wf_args.get("workspace") or "inherit",
        concurrency=int(wf_args.get("concurrency") or wf_args.get("max_workers") or 5),
        max_attempts=int(wf_args.get("max_attempts") or 3),
        lease_seconds=float(wf_args.get("lease_seconds") or 600.0),
        max_claims_per_worker=int(
            1 if wf_args.get("max_claims_per_worker") is None else wf_args["max_claims_per_worker"]
        ),
        conversation_id=wf_args.get("conversation_id") or g.get("CONVERSATION_ID"),
        call_tool_fn=g.get("call_tool"),
        agent_fn=g.get("agent"),
        log_fn=g.get("log"),
        phase_fn=g.get("phase"),
    )


def _install_prelude_coro_hook() -> None:
    """Ensure Antigravity's run_workflow <prelude> _eval awaits returned coroutines."""
    try:
        frame = sys._getframe(1)
        while frame is not None:
            fg = frame.f_globals
            orig_eval = fg.get("_eval")
            if callable(orig_eval) and frame.f_code.co_filename == "<prelude>":
                if not getattr(orig_eval, "_coro_wrapped", False):
                    async def _wrapped_eval(code, scope, _orig=orig_eval):
                        val = await _orig(code, scope)
                        if inspect.iscoroutine(val):
                            val = await val
                        return val
                    _wrapped_eval._coro_wrapped = True  # type: ignore[attr-defined]
                    fg["_eval"] = _wrapped_eval
                break
            frame = frame.f_back
    except Exception:
        pass


def _auto_run_workflow(g: dict[str, Any]) -> Any:
    """Entry point for both Antigravity run_workflow(ScriptPath=...) and CLI execution."""
    if "agent" in g and isinstance(g.get("args"), dict):
        coro = _build_workflow_coro_from_globals(g)
        try:
            asyncio.get_running_loop()
            return coro
        except RuntimeError:
            res = asyncio.run(coro)
            print(json.dumps(res, indent=2))
            return res
    if g.get("__name__") == "__main__":
        return main()
    return None


def main():
    g = globals()
    if "agent" in g and isinstance(g.get("args"), dict):
        result = asyncio.run(_build_workflow_coro_from_globals(g))
        print(json.dumps(result, indent=2))
        return result

    import argparse
    parser = argparse.ArgumentParser(description="Deterministic table workflow runner")
    parser.add_argument("--job-table", dest="job_table", help="Pre-created job table name")
    parser.add_argument("--table", dest="table", help="Source table name")
    parser.add_argument("--template", dest="template", help="Prompt template")
    parser.add_argument("--concurrency", type=int, default=5, help="Worker concurrency limit (default: 5)")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max attempts per task (default: 3)")
    parser.add_argument("--lease-seconds", type=float, default=600.0, help="Lease duration in seconds (default: 600.0)")
    parser.add_argument("--conversation-id", dest="conversation_id", help="Conversation ID")
    cli_args = parser.parse_args()

    result = asyncio.run(run_table_job_workflow(
        job_table=cli_args.job_table,
        table=cli_args.table,
        template=cli_args.template,
        concurrency=cli_args.concurrency,
        max_attempts=cli_args.max_attempts,
        lease_seconds=cli_args.lease_seconds,
        conversation_id=cli_args.conversation_id,
    ))
    print(json.dumps(result, indent=2))
    return result


_install_prelude_coro_hook()
_auto_run_workflow(globals())
