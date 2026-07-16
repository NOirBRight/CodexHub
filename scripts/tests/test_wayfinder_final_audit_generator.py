import base64
import importlib.util
import json
import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate_wayfinder_final_audit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("wayfinder_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class WayfinderAuditGeneratorTests(unittest.TestCase):
    def test_native_capture_requires_exact_uniform_records(self):
        module = load_module()
        capture = json.loads(
            (ROOT / ".superpowers/sdd/wayfinder-native-task-evidence-controller.json").read_text(
                encoding="utf-8"
            )
        )
        normalized = module.validate_native_task_capture(capture, module.FRONTIER_EXPECTED)
        self.assertEqual(len(normalized), 12)
        self.assertTrue(all(record["schema_version"] == 2 for record in normalized))
        self.assertTrue(all(record["match_count"] == 0 for record in normalized))
        self.assertTrue(all(record["unavailable_host_count"] == 0 for record in normalized))

        broken = json.loads(json.dumps(capture))
        broken["records"][0]["query"] = "wrong"
        with self.assertRaisesRegex(ValueError, "query"):
            module.validate_native_task_capture(broken, module.FRONTIER_EXPECTED)

    def test_expected_hotset_parser_preserves_entries_and_builds_matchers(self):
        module = load_module()
        body = """before\n\n## Expected hotset\n\n- `src-python/example.py`\n- one fixture under `tests/fixtures/` if needed\n\n## Relationships\nnone\n"""
        parsed = module.parse_expected_hotset(body, 159)
        self.assertEqual([entry["normalized"] for entry in parsed["entries"]], [
            "src-python/example.py",
            "one fixture under tests/fixtures/ if needed",
        ])
        self.assertIn("src-python/example.py", parsed["matchers"])
        self.assertIn("tests/fixtures/**", parsed["matchers"])

    def test_path_intersections_are_file_based_not_branch_name_based(self):
        module = load_module()
        self.assertEqual(
            module.intersections(
                ["docs/superpowers/plans/example.md", "src-python/codex_proxy.py"],
                ["src-python/codex_proxy.py", "tests/fixtures/**"],
            ),
            ["src-python/codex_proxy.py"],
        )

    def test_settings_drawer_hotset_matches_direct_and_nested_paths_only(self):
        module = load_module()
        matchers = module._path_matchers("the Settings drawer", 111)
        self.assertEqual(
            module.intersections(
                [
                    "frontend/src/components/SettingsDrawer.tsx",
                    "frontend/src/components/settings/SettingsDrawer.tsx",
                    "frontend/src/components/UnrelatedDrawer.tsx",
                ],
                matchers,
            ),
            [
                "frontend/src/components/SettingsDrawer.tsx",
                "frontend/src/components/settings/SettingsDrawer.tsx",
            ],
        )

    def test_task8_comment_helper_supports_create_noop_patch_and_transcripts(self):
        module = load_module()
        plan = (ROOT / "docs/superpowers/plans/2026-07-16-wayfinder-github-migration.md").read_text(
            encoding="utf-8"
        )
        task8 = plan.split("### Task 8:", 1)[1]
        step6 = task8.split("**Step 6:", 1)[1]
        block = re.search(r"```powershell\n(?P<body>.*?)\n```", step6, re.DOTALL)
        self.assertIsNotNone(block)
        prelude = block.group("body").split("$frontierText", 1)[0]

        core_sha = "a" * 64
        desired = (
            module.COMMENT_PREFIXES[147]
            + "\n\nDurable frontier audit: "
            + module.CORE_ARTIFACT_PATH
            + f" (SHA-256: `{core_sha}`)."
        )

        def ps_literal(value):
            return "'" + value.replace("'", "''") + "'"

        harness = "$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest\n" + prelude + rf'''
function Reset-Comments([string]$Body) {{
  if ($null -eq $Body) {{ $script:comments = @() }} else {{
    $script:comments = @([pscustomobject]@{{ id = 4994927680L; body = $Body; html_url = 'https://example.invalid/comment' }})
  }}
}}
function Get-IssueComments([int]$Issue) {{ return @($script:comments) }}
function New-IssueComment([int]$Issue,[string]$Body) {{
  $script:comments = @([pscustomobject]@{{ id = 4994927680L; body = $Body; html_url = 'https://example.invalid/comment' }})
}}
function Update-IssueComment([long]$PublicCommentId,[string]$Body) {{
  $script:comments = @([pscustomobject]@{{ id = $PublicCommentId; body = $Body; html_url = 'https://example.invalid/comment' }})
}}
$desired = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{base64.b64encode(desired.encode('utf-8')).decode('ascii')}'))
$historical = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{base64.b64encode(module.ORIGINAL_CHECKPOINT_BODY.encode('utf-8')).decode('ascii')}'))
$artifactPath = {ps_literal(module.CORE_ARTIFACT_PATH)}
$artifactSha = '{core_sha}'
$old = {ps_literal(module.COMMENT_PREFIXES[147] + chr(10) + chr(10) + 'old checkpoint body')}
$oldHash = Get-NormalizedBodySha256 $old
$cases = @()

Reset-Comments $null
$guard = Set-OrCreateExactIssueComment 147 {ps_literal(module.COMMENT_PREFIXES[147])} $desired 4994927680L $oldHash
$cases += [ordered]@{{ guard = $guard; transcript = New-CheckpointTranscript $guard $historical $desired $artifactPath $artifactSha }}

Reset-Comments $desired
$guard = Set-OrCreateExactIssueComment 147 {ps_literal(module.COMMENT_PREFIXES[147])} $desired 4994927680L $oldHash
$cases += [ordered]@{{ guard = $guard; transcript = New-CheckpointTranscript $guard $historical $desired $artifactPath $artifactSha }}

Reset-Comments $old
$guard = Set-OrCreateExactIssueComment 147 {ps_literal(module.COMMENT_PREFIXES[147])} $desired 4994927680L $oldHash
$cases += [ordered]@{{ guard = $guard; transcript = New-CheckpointTranscript $guard $historical $desired $artifactPath $artifactSha }}

$cases | ConvertTo-Json -Depth 10 -Compress
'''
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=harness,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip(), completed.stderr or "PowerShell harness produced no JSON")
        cases = json.loads(completed.stdout)
        self.assertEqual(
            [case["guard"]["operation_decision"] for case in cases],
            ["create", "exact-no-op", "patch"],
        )
        expected_keys = set(cases[0]["guard"])
        self.assertTrue(all(set(case["guard"]) == expected_keys for case in cases))
        self.assertIsNone(cases[0]["guard"]["prior_public_comment_id"])
        self.assertIsNone(cases[0]["guard"]["prior_normalized_body_sha256"])
        self.assertTrue(all(case["guard"]["post_prefix_multiplicity"] == 1 for case in cases))
        self.assertTrue(all(case["guard"]["post_exact_body_multiplicity"] == 1 for case in cases))
        for case in cases:
            with self.subTest(operation=case["guard"]["operation_decision"]):
                if case["guard"]["operation_decision"] != "create":
                    self.assertEqual(
                        module.normalize_body(case["transcript"]["historical_original"]["normalized_body"]),
                        module.normalize_body(module.ORIGINAL_CHECKPOINT_BODY),
                    )
                module.validate_transcript(case["transcript"], core_sha)

    def test_initial_checkpoint_transcript_finalizes_without_prior_state(self):
        module = load_module()
        core = json.loads(
            (ROOT / module.CORE_ARTIFACT_PATH).read_text(encoding="utf-8")
        )
        core["global_github_audit"]["comments"]["147"] = None
        core_sha = module.serialized_json_sha256(core)
        desired_body = (
            module.COMMENT_PREFIXES[147]
            + "\n\nDurable frontier audit: "
            + module.CORE_ARTIFACT_PATH
            + f" (SHA-256: `{core_sha}`)."
        )
        desired_hash = module.sha256_text(desired_body)
        transcript = {
            "schema_version": 1,
            "captured_at": "2026-07-17T00:00:00Z",
            "issue": 147,
            "purpose_prefix": module.COMMENT_PREFIXES[147],
            "historical_original": {
                "source": "Task 8 initial publication desired body",
                "normalized_body": desired_body,
                "normalized_body_sha256": desired_hash,
            },
            "pre_update": {
                "public_comment_id": None,
                "normalized_body_sha256": None,
                "readback_at": None,
            },
            "desired": {
                "normalized_body": desired_body,
                "normalized_body_sha256": desired_hash,
                "frontier_artifact_path": module.CORE_ARTIFACT_PATH,
                "frontier_artifact_sha256": core_sha,
            },
            "operation_decision": "create",
            "post_readback": {
                "public_comment_id": 1,
                "normalized_body_sha256": desired_hash,
                "prefix_multiplicity": 1,
                "exact_body_multiplicity": 1,
                "readback_at": "2026-07-17T00:00:01Z",
            },
        }
        final = module.build_final(core, module.CORE_ARTIFACT_PATH, transcript)
        self.assertEqual(final["derivation"]["checkpoint_exact_body_guard"], "create")


if __name__ == "__main__":
    unittest.main()
