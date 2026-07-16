import base64
import importlib.util
import json
import pathlib
import re
import subprocess
import tempfile
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
$proof = Resolve-ProvenCheckpointHistoricalProvenance $guard $desired $null $historical
$cases += [ordered]@{{ guard = $guard; transcript = New-CheckpointTranscript $guard $proof $desired $artifactPath $artifactSha }}

Reset-Comments $desired
$guard = Set-OrCreateExactIssueComment 147 {ps_literal(module.COMMENT_PREFIXES[147])} $desired 4994927680L $oldHash
$proof = Resolve-ProvenCheckpointHistoricalProvenance $guard $desired $null $historical
$cases += [ordered]@{{ guard = $guard; transcript = New-CheckpointTranscript $guard $proof $desired $artifactPath $artifactSha }}

Reset-Comments $old
$guard = Set-OrCreateExactIssueComment 147 {ps_literal(module.COMMENT_PREFIXES[147])} $desired 4994927680L $oldHash
$proof = Resolve-ProvenCheckpointHistoricalProvenance $guard $desired $null $historical
$cases += [ordered]@{{ guard = $guard; transcript = New-CheckpointTranscript $guard $proof $desired $artifactPath $artifactSha }}

$cases | ConvertTo-Json -Depth 10 -Compress
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            harness_path = pathlib.Path(temp_dir) / "checkpoint-sequence.ps1"
            harness_path.write_text(harness, encoding="utf-8")
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness_path)],
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
                if case["guard"]["operation_decision"] == "patch":
                    self.assertEqual(
                        module.normalize_body(case["transcript"]["historical_original"]["normalized_body"]),
                        module.normalize_body(module.ORIGINAL_CHECKPOINT_BODY),
                    )
                elif case["guard"]["operation_decision"] == "exact-no-op":
                    self.assertEqual(
                        module.normalize_body(case["transcript"]["historical_original"]["normalized_body"]),
                        desired,
                    )
                module.validate_transcript(case["transcript"], core_sha)
        broken_noop = json.loads(json.dumps(cases[1]["transcript"]))
        broken_noop["pre_update"]["normalized_body_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "exact-no-op pre/desired/post hashes differ"):
            module.validate_transcript(broken_noop, core_sha)

        broken_origin = json.loads(json.dumps(cases[1]["transcript"]))
        broken_origin["historical_original"] = {
            "source": "Task 8 execution report exact original published body",
            "normalized_body": module.ORIGINAL_CHECKPOINT_BODY,
            "normalized_body_sha256": module.sha256_text(module.ORIGINAL_CHECKPOINT_BODY),
        }
        with self.assertRaisesRegex(ValueError, "live-exact provenance mismatch"):
            module.validate_transcript(broken_origin, core_sha)

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
            "historical_provenance": {
                "proof_kind": "initial-create",
                "prior_transcript": None,
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

    def test_stateful_retries_preserve_proven_historical_origin(self):
        module = load_module()
        plan = (ROOT / "docs/superpowers/plans/2026-07-16-wayfinder-github-migration.md").read_text(
            encoding="utf-8"
        )
        task8 = plan.split("### Task 8:", 1)[1]
        step6 = task8.split("**Step 6:", 1)[1]
        block = re.search(r"```powershell\n(?P<body>.*?)\n```", step6, re.DOTALL)
        self.assertIsNotNone(block)
        prelude = block.group("body").split("$frontierText", 1)[0]

        tracked_core = json.loads((ROOT / module.CORE_ARTIFACT_PATH).read_text(encoding="utf-8"))
        create_core = json.loads(json.dumps(tracked_core))
        create_core["global_github_audit"]["comments"]["147"] = None
        create_core_sha = module.serialized_json_sha256(create_core)
        create_desired = (
            module.COMMENT_PREFIXES[147]
            + "\n\nDurable frontier audit: "
            + module.CORE_ARTIFACT_PATH
            + f" (SHA-256: `{create_core_sha}`)."
        )

        patch_core = json.loads(json.dumps(tracked_core))
        patch_prior_body = module.COMMENT_PREFIXES[147] + "\n\nPrior checkpoint body."
        patch_core["global_github_audit"]["comments"]["147"][
            "normalized_body_sha256"
        ] = module.sha256_text(patch_prior_body)
        patch_core_sha = module.serialized_json_sha256(patch_core)
        self.assertEqual(
            module.sha256_text(patch_prior_body),
            patch_core["global_github_audit"]["comments"]["147"]["normalized_body_sha256"],
        )
        patch_desired = (
            module.COMMENT_PREFIXES[147]
            + "\n\nDurable frontier audit: "
            + module.CORE_ARTIFACT_PATH
            + f" (SHA-256: `{patch_core_sha}`).\n\nStateful retry target."
        )

        def b64(value):
            return base64.b64encode(value.encode("utf-8")).decode("ascii")

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
$prefix = '{module.COMMENT_PREFIXES[147]}'
$artifactPath = '{module.CORE_ARTIFACT_PATH}'
$legacy = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64(module.ORIGINAL_CHECKPOINT_BODY)}'))
$createDesired = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64(create_desired)}'))
$patchPrior = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64(patch_prior_body)}'))
$patchDesired = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64(patch_desired)}'))

Reset-Comments $null
$createGuard = Set-OrCreateExactIssueComment 147 $prefix $createDesired 4994927680L ('0' * 64)
$createProof = Resolve-ProvenCheckpointHistoricalProvenance $createGuard $createDesired $null $legacy
$createTranscript = New-CheckpointTranscript $createGuard $createProof $createDesired $artifactPath '{create_core_sha}'
$createNoopGuard = Set-OrCreateExactIssueComment 147 $prefix $createDesired 4994927680L ('0' * 64)
$createNoopProof = Resolve-ProvenCheckpointHistoricalProvenance $createNoopGuard $createDesired $createTranscript $legacy
$createNoopTranscript = New-CheckpointTranscript $createNoopGuard $createNoopProof $createDesired $artifactPath '{create_core_sha}'

Reset-Comments $patchPrior
$patchPriorHash = Get-NormalizedBodySha256 $patchPrior
$patchGuard = Set-OrCreateExactIssueComment 147 $prefix $patchDesired 4994927680L $patchPriorHash
$patchProof = Resolve-ProvenCheckpointHistoricalProvenance $patchGuard $patchDesired $null $legacy
$patchTranscript = New-CheckpointTranscript $patchGuard $patchProof $patchDesired $artifactPath '{patch_core_sha}'
$patchNoopGuard = Set-OrCreateExactIssueComment 147 $prefix $patchDesired 4994927680L $patchPriorHash
$patchNoopProof = Resolve-ProvenCheckpointHistoricalProvenance $patchNoopGuard $patchDesired $patchTranscript $legacy
$patchNoopTranscript = New-CheckpointTranscript $patchNoopGuard $patchNoopProof $patchDesired $artifactPath '{patch_core_sha}'

$result = [ordered]@{{
  create = $createTranscript
  create_noop = $createNoopTranscript
  patch = $patchTranscript
  patch_noop = $patchNoopTranscript
}}
$json = $result | ConvertTo-Json -Depth 30 -Compress
if ([string]::IsNullOrWhiteSpace($json)) {{ throw 'PowerShell sequence JSON serialization returned empty output' }}
Write-Output $json
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            harness_path = pathlib.Path(temp_dir) / "checkpoint-sequence.ps1"
            harness_path.write_text(harness, encoding="utf-8")
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness_path)],
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=ROOT,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip(), completed.stderr or "PowerShell sequence produced no JSON")
        transcripts = json.loads(completed.stdout)

        for first, retry, core in (
            ("create", "create_noop", create_core),
            ("patch", "patch_noop", patch_core),
        ):
            self.assertEqual(
                transcripts[first]["historical_original"],
                transcripts[retry]["historical_original"],
            )
            self.assertEqual(transcripts[retry]["operation_decision"], "exact-no-op")
            hashes = {
                transcripts[retry]["pre_update"]["normalized_body_sha256"],
                transcripts[retry]["desired"]["normalized_body_sha256"],
                transcripts[retry]["post_readback"]["normalized_body_sha256"],
            }
            self.assertEqual(len(hashes), 1)
            module.build_final(core, module.CORE_ARTIFACT_PATH, transcripts[first])
            module.build_final(core, module.CORE_ARTIFACT_PATH, transcripts[retry])

        self.assertIsNone(transcripts["create"]["pre_update"]["normalized_body_sha256"])
        self.assertEqual(
            transcripts["create"]["historical_original"]["normalized_body"],
            create_desired,
        )
        self.assertEqual(
            transcripts["patch"]["pre_update"]["normalized_body_sha256"],
            module.sha256_text(patch_prior_body),
        )


if __name__ == "__main__":
    unittest.main()
