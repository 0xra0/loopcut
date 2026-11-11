# Local evals: Loopcut on a model on this computer

Loopcut's hosted eval suite (`harness/evals/`) is not in the public tree, and its orchestrator, router and workers
run only on loopcut.org. These evals measure the part that does run locally: the single agent, talking to
Strata (or any OpenAI-compatible server on 127.0.0.1), taught by the skills in `LOOPCUT_SKILLS_DIR`.

| File | What it does |
| --- | --- |
| `tasks.py` | 15 tasks (including Pixal3D armor repair and debris cleanup): prompt, starting scene, a check that reads the evaluated scene, a reference solution |
| `selfcheck.py` | Every check fails on the untouched scene and passes on its reference. No model. ~5 s |
| `run_task.py` | One task as a real agent turn in a GUI Blender (the way `harness/live_check.py` runs one) |
| `run.py` | All tasks, each in its own Blender; writes `results.json`, `summary.md`, transcripts, and quality metrics report |

## Quality Tracking Metrics Over Time

The evaluation runner tracks key quality and performance dimensions:
- **Completion rate (`completion_rate_pct`)**: Percentage of tasks satisfying all objective scene acceptance checks.
- **Tool-call errors (`tool_call_errors`)**: Count of model schema mismatches, bad arguments, or runtime tool failures.
- **Geometry damage (`total_geometry_damage`)**: Measured surface distortion or unintended mesh degradation.
- **Elapsed time (`total_elapsed_s`)**: Execution latency per task and cumulative workflow duration.
- **VRAM footprint (`vram_used_mib`)**: GPU memory usage tracked via `gpu_coordinator` ROCm sysfs before and after stages.

```sh
# no model: are the checks sound?
blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/local_evals/selfcheck.py
# Strata on :8099 must be up; Blender must not get ~/.local/bin on PATH (run.py strips it)
python3 loopcut/harness/local_evals/run.py --label "IQ2_XS + skills v1"
python3 loopcut/harness/local_evals/run.py --label "IQ2_XS, no skills" --no-skills   # what the skills are worth
python3 loopcut/harness/local_evals/run.py longsword uv_unwrap                         # some tasks only
```

Runs land in `/mnt/ssd/work/loopcut-local/runs/<time>/` (`LOCAL_EVAL_RUNS`). Each `summary.md` compares itself
with the previous run, so label what changed. A run folder is in the format `harness/distill.py packet` reads, so a
passing task can be drafted into a new skill:

```sh
python3 loopcut/harness/distill.py packet /mnt/ssd/work/loopcut-local/runs/<time> --out .distill-out
python3 loopcut/harness/distill.py draft .distill-out/FILE.packet.md --out .distill-out/drafts --drafter codex
python3 loopcut/harness/distill.py check .distill-out/drafts/distilled/ID.md   # then copy it to local-skills/
```

## Skills

`/mnt/sata/loopcut/local-skills/*.md` (the default `LOOPCUT_SKILLS_DIR` here): `part: single` skills, inlined as
`<craft_notes>` on the message that starts a single-agent turn when their `words` regex matches the request
(`agent._single_turn`). `words: .` matches every request. Keep each body under 3,060 characters, and write down only
facts checked in this Blender (`/mnt/ssd/work/loopcut-local/verify_facts*.py`) or in game.

Rules for a task: its check must read the scene (evaluated, world space), never the transcript; it must fail on the
untouched scene and pass on its reference (`selfcheck.py`); and it must accept any correct solution, not just the
reference's method.
