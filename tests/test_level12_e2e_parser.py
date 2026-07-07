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
                completed_call("spawn_agent", "You are the implementer subagent."),
                completed_call("wait", None),
                completed_call("close_agent", None),
                completed_call("spawn_agent", "You are the spec reviewer subagent."),
                completed_call("wait", None),
                completed_call("close_agent", None),
                completed_call("spawn_agent", "You are the code-quality reviewer subagent."),
                completed_call("wait", None),
                completed_call("close_agent", None),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            f"SENTINEL: {sentinel}\n"
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


def completed_call(tool, prompt):
    return {
        "type": "item.completed",
        "item": {
            "type": "collab_tool_call",
            "tool": tool,
            "status": "completed",
            "prompt": prompt,
            "receiver_thread_ids": ["agent-1"] if tool == "spawn_agent" else [],
        },
    }


if __name__ == "__main__":
    unittest.main()
