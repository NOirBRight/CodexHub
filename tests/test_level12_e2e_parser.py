import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def load_runner_module():
    module_path = Path(__file__).resolve().parents[1] / "diagnostics" / "subagent-e2e" / "run_level12_e2e.py"
    spec = importlib.util.spec_from_file_location("run_level12_e2e", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Level12E2EParserTests(unittest.TestCase):
    def test_level2_analyzer_accepts_current_prompt_artifact_shape(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            output_path = root / "level2-m3-responses.artifact-r01.txt"
            sentinel = "SENTINEL:level2-m3-responses-20260706"
            output_path.write_text(
                "case: level2-m3-responses\n"
                "model: minimax-m3\n"
                "endpoint: responses\n"
                f"{sentinel}\n"
                "artifact: ok\n",
                encoding="utf-8-sig",
                newline="\n",
            )
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "You are the implementer subagent.", receivers=["agent-impl"]),
                completed_call("wait", None, receivers=["agent-impl"]),
                completed_call("close_agent", None, receivers=["agent-impl"]),
                completed_call("spawn_agent", "You are the spec reviewer subagent.", receivers=["agent-spec"]),
                completed_call("wait", None, receivers=["agent-spec"]),
                completed_call("close_agent", None, receivers=["agent-spec"]),
                completed_call("spawn_agent", "You are the code-quality reviewer subagent.", receivers=["agent-quality"]),
                completed_call("wait", None, receivers=["agent-quality"]),
                completed_call("close_agent", None, receivers=["agent-quality"]),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            f"{sentinel}\n"
                            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level2-m3-responses-r01",
                "model": "ollama-e2e-responses/minimax-m3",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level2(case, output_path, sentinel)

            self.assertTrue(summary["checks"]["artifact_exact"])
            self.assertTrue(summary["pass"])
            self.assertEqual(
                summary["expected_artifact_text"],
                output_path.read_text(encoding="utf-8-sig"),
            )

    def test_level2_analyzer_accepts_final_sentinel_as_complete_line(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            output_path = root / "level2-k2_7-responses.artifact-r03.txt"
            sentinel = "SENTINEL:level2-k2_7-responses-20260706"
            output_path.write_text(
                "case: level2-k2_7-responses\n"
                "model: kimi-k2.7-code\n"
                "endpoint: responses\n"
                f"{sentinel}\n"
                "artifact: ok\n",
                encoding="utf-8",
                newline="\n",
            )
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "You are the implementer subagent.", receivers=["agent-impl"]),
                completed_call("wait", None, receivers=["agent-impl"]),
                completed_call("close_agent", None, receivers=["agent-impl"]),
                completed_call("spawn_agent", "You are the spec reviewer subagent.", receivers=["agent-spec"]),
                completed_call("wait", None, receivers=["agent-spec"]),
                completed_call("close_agent", None, receivers=["agent-spec"]),
                completed_call("spawn_agent", "You are the code-quality reviewer subagent.", receivers=["agent-quality"]),
                completed_call("wait", None, receivers=["agent-quality"]),
                completed_call("close_agent", None, receivers=["agent-quality"]),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            f"{sentinel}\n"
                            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level2-k2_7-responses-r03",
                "model": "ollama-e2e-responses/kimi-k2.7-code",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level2(case, output_path, sentinel)

            self.assertTrue(summary["checks"]["artifact_exact"])
            self.assertTrue(summary["checks"]["final_exact"])
            self.assertTrue(summary["pass"])

    def test_level2_analyzer_rejects_reviewers_spawned_before_implementer_closed(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            output_path = root / "level2-m3-responses.artifact-r01.txt"
            sentinel = "SENTINEL:level2-m3-responses-20260706"
            output_path.write_text(
                "case: level2-m3-responses\n"
                "model: minimax-m3\n"
                "endpoint: responses\n"
                f"{sentinel}\n"
                "artifact: ok\n",
                encoding="utf-8",
                newline="\n",
            )
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "You are the implementer subagent.", receivers=["agent-impl"]),
                completed_call("spawn_agent", "You are the spec reviewer subagent.", receivers=["agent-spec"]),
                completed_call("spawn_agent", "You are the code-quality reviewer subagent.", receivers=["agent-quality"]),
                completed_call("wait", None, receivers=["agent-impl"]),
                completed_call("wait", None, receivers=["agent-spec"]),
                completed_call("wait", None, receivers=["agent-quality"]),
                completed_call("close_agent", None, receivers=["agent-impl"]),
                completed_call("close_agent", None, receivers=["agent-spec"]),
                completed_call("close_agent", None, receivers=["agent-quality"]),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            f"{sentinel}\n"
                            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level2-m3-responses-r01",
                "model": "ollama-e2e-responses/minimax-m3",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level2(case, output_path, sentinel)

            self.assertFalse(summary["checks"].get("role_lifecycle_order", True))
            self.assertFalse(summary["pass"])

    def test_level3_analyzer_accepts_parallel_branch_order(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "Node: task-a-implementer", receivers=["agent-a"]),
                completed_call("wait", None, messages={"agent-a": "A_DONE"}, receivers=["agent-a"]),
                completed_call("close_agent", None, receivers=["agent-a"]),
                completed_call("spawn_agent", "Node: task-b-implementer", receivers=["agent-b"]),
                completed_call("spawn_agent", "Node: task-a-reviewer", receivers=["agent-review"]),
                completed_call(
                    "wait",
                    None,
                    messages={"agent-b": "B_DONE", "agent-review": "A_REVIEW_PASS"},
                    receivers=["agent-b", "agent-review"],
                ),
                completed_call("close_agent", None, receivers=["agent-b"]),
                completed_call("close_agent", None, receivers=["agent-review"]),
                completed_call("spawn_agent", "Node: final-summarizer", receivers=["agent-final"]),
                completed_call("wait", None, messages={"agent-final": "FINAL_READY"}, receivers=["agent-final"]),
                completed_call("close_agent", None, receivers=["agent-final"]),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            "DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer\n"
                            "DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level3-m3-responses-r01",
                "model": "ollama-e2e-responses/minimax-m3",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level3_dynamic_dag(case)

            self.assertTrue(summary["checks"]["branch_nodes_seen"])
            self.assertTrue(summary["checks"]["final_exact"])
            self.assertTrue(summary["pass"])

    def test_level3_analyzer_rejects_final_summarizer_before_branch_closes(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "Node: task-a-implementer", receivers=["agent-a"]),
                completed_call("wait", None, messages={"agent-a": "A_DONE"}, receivers=["agent-a"]),
                completed_call("close_agent", None, receivers=["agent-a"]),
                completed_call("spawn_agent", "Node: task-a-reviewer", receivers=["agent-review"]),
                completed_call("spawn_agent", "Node: task-b-implementer", receivers=["agent-b"]),
                completed_call("spawn_agent", "Node: final-summarizer", receivers=["agent-final"]),
                completed_call("wait", None, messages={"agent-review": "A_REVIEW_PASS"}, receivers=["agent-review"]),
                completed_call("wait", None, messages={"agent-b": "B_DONE"}, receivers=["agent-b"]),
                completed_call("wait", None, messages={"agent-final": "FINAL_READY"}, receivers=["agent-final"]),
                completed_call("close_agent", None, receivers=["agent-review"]),
                completed_call("close_agent", None, receivers=["agent-b"]),
                completed_call("close_agent", None, receivers=["agent-final"]),
                completed_call("close_agent", None, receivers=["agent-a"]),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            "DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer\n"
                            "DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level3-m3-responses-r01",
                "model": "ollama-e2e-responses/minimax-m3",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level3_dynamic_dag(case)

            self.assertFalse(summary["checks"].get("dependency_order", True))
            self.assertFalse(summary["pass"])

    def test_level2_analyzer_rejects_wrong_agent_lifecycle_completion(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            output_path = root / "level2-m3-responses.artifact-r01.txt"
            sentinel = "SENTINEL:level2-m3-responses-20260706"
            output_path.write_text(
                "case: level2-m3-responses\n"
                "model: minimax-m3\n"
                "endpoint: responses\n"
                f"{sentinel}\n"
                "artifact: ok\n",
                encoding="utf-8",
                newline="\n",
            )
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "You are the implementer subagent.", receivers=["agent-impl"]),
                completed_call("wait", None, receivers=["agent-other"]),
                completed_call("close_agent", None, receivers=["agent-other"]),
                completed_call("spawn_agent", "You are the spec reviewer subagent.", receivers=["agent-spec"]),
                completed_call("wait", None, receivers=["agent-spec"]),
                completed_call("close_agent", None, receivers=["agent-spec"]),
                completed_call("spawn_agent", "You are the code-quality reviewer subagent.", receivers=["agent-quality"]),
                completed_call("wait", None, receivers=["agent-quality"]),
                completed_call("close_agent", None, receivers=["agent-quality"]),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            f"{sentinel}\n"
                            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level2-m3-responses-r01",
                "model": "ollama-e2e-responses/minimax-m3",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level2(case, output_path, sentinel)

            self.assertFalse(summary["checks"].get("role_lifecycle_order", True))
            self.assertFalse(summary["pass"])


def completed_call(tool, prompt, messages=None, receivers=None):
    if receivers is None:
        receivers = ["agent-1"] if tool == "spawn_agent" else []
    item = {
        "type": "collab_tool_call",
        "tool": tool,
        "status": "completed",
        "prompt": prompt,
        "receiver_thread_ids": receivers,
    }
    if messages is not None:
        item["agents_states"] = {
            agent_id: {"status": "completed", "message": message}
            for agent_id, message in messages.items()
        }
    return {"type": "item.completed", "item": item}


if __name__ == "__main__":
    unittest.main()
