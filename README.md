<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/lockup-dark.png">
    <img alt="Loopcut" src=".github/readme/lockup-light.png" width="340">
  </picture>
</p>

<h3 align="center">Autonomous AI Agent inside Blender 5.2 — Local AI & Studio Edition</h3>

<p align="center">
  Describe 3D tasks in plain English. Loopcut reasons, writes and verifies Blender Python, confirms destructive actions, captures the viewport to evaluate results, and executes with one-click checkpoint rollback.
</p>

<p align="center">
  <a href="#key-features--enhancements">Features</a> ·
  <a href="#quick-start--installation">Install</a> ·
  <a href="#building-from-source-linux">Build from Source</a> ·
  <a href="#model-providers--configuration">Models</a> ·
  <a href="#studio-tools--playbooks">Studio Tools</a> ·
  <a href="#local-evaluations--skills">Local Evals</a> ·
  <a href="#testing">Testing</a>
</p>

<p align="center">
  <a href="https://github.com/0xra0/loopcut/releases/latest"><img alt="Latest Release" src="https://img.shields.io/github/v/release/0xra0/loopcut?style=flat-square&color=4a95a3&label=release"></a>
  <img alt="Blender 5.2.2 LTS" src="https://img.shields.io/badge/Blender-5.2.2%20LTS-4a95a3?style=flat-square">
  <img alt="Platforms: Linux | macOS | Windows" src="https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-2a2a2e?style=flat-square">
  <img alt="Default Local AI: Strata Swift 1.5" src="https://img.shields.io/badge/local%20AI-Strata%20Swift%201.5-brightgreen?style=flat-square">
  <img alt="Privacy: Local First" src="https://img.shields.io/badge/privacy-local%20first-blue?style=flat-square">
  <a href="LICENSE"><img alt="License: GPL-3.0-or-later" src="https://img.shields.io/badge/license-GPL--3.0--or--later-2a2a2e?style=flat-square"></a>
</p>

<p align="center">
  <img alt="Loopcut inside Blender" src=".github/readme/hero.webp" width="960">
</p>

---

## What is Loopcut?

**You're the artist. Loopcut's the technician.**

Loopcut integrates a specialized autonomous AI agent directly into Blender 5.2. Instead of writing boilerplate scripts, debugging API changes, or manually clicking through nested modifier stacks and topology operations, describe your technical objective in a sentence. Loopcut:

1. **Inspects the Scene**: Queries active meshes, modifier stacks, materials, geometry node trees, constraints, animation data, and camera framing in bulk.
2. **Generates & Normalizes Code**: Produces exact Blender 5.2 Python actions with automatic syntax normalization and repair for complex tool arguments.
3. **Requests Approval for Destructive Steps**: Shows code diffs and waits for your consent before executing changes.
4. **Verifies Viewport & Evaluated Meshes**: Captures the viewport from optimal angles and compares against reference images or geometric invariants.
5. **Enables Instant Rollback**: Creates non-destructive undo checkpoints before every turn without modifying your saved `.blend` file.

---

## Key Features & Modifications in this Edition

This fork enhances the upstream Loopcut release with offline local intelligence, advanced studio toolsets, privacy protections, and dedicated Linux build improvements:

### 1. Local AI by Default (Strata Swift 1.5)
- **Zero Cloud Dependence**: Configured out of the box to communicate with **Strata** (Swift 1.5 on Strata's engine at `127.0.0.1:8099`) as the primary provider and fallback.
- **Thinking Mode Selector**: Switch between **Fast** (low reasoning effort), **Balanced** (medium reasoning effort), and **Deep** (high reasoning effort) directly from the N-panel.
- **Non-blocking Health Check**: Added a *Test Strata* button and real-time status reporting that will never hang or block Blender's UI thread during drawing.

### 2. Antigravity CLI (AGY) Integration
- Direct native support for the **Antigravity CLI (AGY)** provider (`scripts/addons_core/loopcut/agy_cli.py`).
- Automatic argument repair for tool calls: automatically fixes `run_python` calls containing literal newlines or escaped formatting artifacts common in local LLM tool responses.

### 3. Claude Blender Integration & Studio Tools
- Embedded command handlers from the Claude Blender Integration suite without requiring extra localhost sockets or MCP daemons for chat:
  - **`studio_guide`**: Browse structured command groups, inspect parameters, docstrings, and run staged playbooks (`retopo_and_bake`, `refine_generated`, `mesh_report`).
  - **`studio_action`**: Execute advanced studio handlers on Blender's main thread with full scene diffing and Loopcut undo checkpoints.
  - **Specialized Toolsets**: Fast Sculpting (`fast_sculpt`), Hard Surface booleans & bevels (`hard_surface`), Quad Retopology (`retopo_tools`), Geometry Nodes orchestration (`geonodes_controller`), and Rigging & Animation (`rig_anim_controller`).

### 4. Pixal3D Multiview 3D Pipeline
- Bundled Pixal3D multiview generation pipeline for turning multi-angle image references into initial 3D mesh representations inside Blender.

### 5. Local Privacy by Default
- Usage telemetry and conversation log sharing are disabled by default (`scripts/addons_core/loopcut/privacy.py`). Your scenes, reference photos, geometry, and conversation data remain strictly on your local machine.

### 6. Dynamic Add-on Discovery & Reach
- Detects installed Blender add-ons that are disabled and counts them per request.
- The agent can search disabled add-ons via `inspect_api` and dynamically activate an add-on for the current session when needed, without permanently changing your Blender startup configuration.

### 7. Linux System Glog & Build Fixes
- Fixes dual-glog / gflags symbol collision crashes (`-DWITH_SYSTEM_GLOG=ON`, `-DWITH_SYSTEM_GFLAGS=ON`).
- Includes a turn-key build automation script (`build-loopcut.sh`) that cleanly handles headless builds, Python `bpy` modules, and full GUI Blender with AMD ROCm / HIP support.

---

## Quick Start & Installation

### Option A: Download GitHub Release
Pre-packaged release builds are available on the [Releases page](https://github.com/0xra0/loopcut/releases):

| Platform | Package | Notes |
|---|---|---|
| **Linux (x86_64)** | `loopcut-linux-x86_64.tar.gz` | Includes standalone Blender GUI binary with local AI enhancements. |
| **Linux Patches & Scripts** | `build-loopcut.sh`, `fix-loopcut-glog-v2.sh` | Turn-key scripts for building from source on any modern distribution. |

### Option B: Building from Source (Linux)

Use the included `build-loopcut.sh` script to configure and compile in isolated directories:

```bash
# 1. Clone the repository
git clone https://github.com/0xra0/loopcut.git
cd loopcut

# 2. Build full GUI Blender (adjust -j for CPU cores)
./build-loopcut.sh --full -j $(nproc)

# 3. Test built executable
./build-full-glogfix/bin/blender --version

# 4. (Optional) Run the Blender-free test suite
./build-loopcut.sh --tests

# 5. Launch Loopcut
./build-full-glogfix/bin/blender
```

#### Additional Build Profiles:
```bash
# Build headless Blender (for batch rendering / CI)
./build-loopcut.sh --headless -j 8

# Build the Python 'bpy' standalone module
./build-loopcut.sh --py -j 8

# Build all three profiles with AMD HIP/ROCm acceleration
./build-loopcut.sh --all --hip on -j 8

# Preview build commands without running
./build-loopcut.sh --dry-run
```

---

## How to Use Loopcut

### 1. First Launch & Setup
1. Launch Loopcut Blender.
2. In the splash screen, select **Import Blender 5.2 Settings** to migrate your existing preferences, keymaps, and add-ons, or choose **Start Fresh**.
3. Open the 3D Viewport. The Loopcut panel is docked on the right side of the viewport. Toggle it anytime with <kbd>Ctrl</kbd> + <kbd>L</kbd> (or <kbd>⌘</kbd> + <kbd>L</kbd> on macOS).

### 2. Configure Your AI Engine
In the Loopcut panel header or under *Preferences › Add-ons › Loopcut*:
- **Local Strata (Default)**: Ensure Strata is running on port 8099 (`http://127.0.0.1:8099/v1`). Click **Test Strata** to verify connectivity. Select your preferred Thinking Mode (*Fast*, *Balanced*, or *Deep*).
- **Antigravity CLI (AGY)**: Select the AGY provider to route commands through your local Antigravity installation.
- **Custom / Cloud Models**: You can also enter an API key for Anthropic (Claude 3.5 Sonnet / Opus) or any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, OpenRouter).

### 3. Example Workflows & Prompts

#### Modeling & Modifiers
```
Create a medieval wooden chest: bevel the edges, add iron strap bands with mirror modifiers, and ensure transform origins are at the base.
```
```
Clean up non-manifold geometry, merge vertices within 0.001m, and recalculate all normals facing outward.
```

#### Studio Tools & Retopology
```
Inspect the active character mesh with studio_guide, run the retopo_and_bake playbook, and generate an 8k normal map from high-poly sculpt details.
```
```
Use studio_action to apply a subdivision remesh on the active sculpt and unwrap seams along sharp edges.
```

#### Lighting, Camera & Scene Setup
```
Match the camera framing to the reference photo I dropped in, and set up a three-point softbox studio lighting rig with warm key light and cool rim light.
```
```
Sort all mesh objects in the scene into collections by material, and stack Bevel before Subdivision Surface across Props.
```

#### Procedural & Geometry Nodes
```
Create a Geometry Nodes tree on the selected plane that distributes pebbles and grass instances with random scale and rotation seeded by vertex weight.
```

---

## Studio Tools & Playbooks

This edition vendors the **Claude Blender Integration** commands under `scripts/addons_core/loopcut/studio_vendor/`:

| Command / Tool | Purpose |
|---|---|
| `studio_guide(toolset="...")` | Lists commands and signatures for `look`, `modeling`, `topology`, `sculpt`, `uv_bake`, `materials`, `rig`, `animate`, or `export`. |
| `studio_guide(playbook="...")` | Retrieves multi-step execution playbooks (`retopo_and_bake`, `refine_generated`, `mesh_report`). |
| `studio_action(command, params)` | Dispatches validated operations with automatic undo checkpointing and scene diff calculation. |
| `fast_sculpt` / `sculpt_controller` | High-performance stroke injection, remeshing, and dynamic topology manipulation. |
| `hard_surface` | Boolean solver automation, chamfers, weighted normals, and non-destructive modifier stacks. |
| `geonodes_controller` | Programmatic node tree synthesis, socket wiring, and exposed attribute bindings. |

---

## Local Evaluations & Skills

### Running Local Evals
Loopcut includes a benchmark harness under `loopcut/harness/local_evals/` designed to evaluate offline local models against concrete 3D modeling tasks:

```bash
# Verify all task evaluations against ground-truth references (no model needed; ~5s)
./build-full-glogfix/bin/blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/local_evals/selfcheck.py

# Evaluate a local model on port 8099
python3 loopcut/harness/local_evals/run.py --label "Strata Swift 1.5 + skills"

# Evaluate specific tasks
python3 loopcut/harness/local_evals/run.py longsword uv_unwrap
```

### Custom Craft Skills
Domain-specific modeling knowledge is stored as modular Markdown skills in `local-skills/`:
- `blender-52-api.md`: Up-to-date Python API patterns for Blender 5.2.
- `geometry-nodes.md`: Recipes and node type hierarchies for procedural setups.
- `redengine-export.md`: Constraints and hierarchy conventions for game asset export.

---

## Testing

Over 4,700 unit tests verify the agent loop, context compression, checkpoint safety, and tool handlers without requiring a GUI:

```bash
# Run all loopcut unit tests
python3 -m unittest discover -s loopcut/tests -v

# Run targeted local AI & studio test suites
python3 -m unittest loopcut/tests/test_local_ai_and_advanced_suite.py
python3 -m unittest loopcut/tests/test_agy.py
python3 -m unittest loopcut/tests/test_studio_integration.py
```

---

## Repository Structure

```
├── build-loopcut.sh               # Turn-key Linux build script (headless, bpy, full)
├── fix-loopcut-glog-v2.sh         # System glog / gflags duplicate symbol fix script
├── local-skills/                  # Custom domain knowledge skills for local models
├── loopcut/
│   ├── CLAUDE_BLENDER_INTEGRATION.md  # Studio tools integration guide
│   ├── DEVELOPING.md              # Developer architecture documentation
│   ├── harness/                   # Local evaluations & distillation pipelines
│   │   └── local_evals/           # 13 verified scene benchmark tasks
│   └── tests/                     # 4,700+ offline unit tests
├── scripts/addons_core/loopcut/   # Main Loopcut Blender add-on source
│   ├── agent.py                   # Multi-turn reasoning loop & consent handling
│   ├── agy_cli.py                 # Antigravity CLI provider implementation
│   ├── llm.py                     # Provider adapters (Strata, OpenAI, Anthropic)
│   ├── privacy.py                 # Local privacy enforcement
│   ├── studio_vendor/             # Claude Blender Integration command handlers
│   └── tools.py                   # Python runner, scene inspection & viewport tools
├── source/                        # Blender 5.2 C/C++ core fork
└── release/                       # UI branding, bundled extensions & presets
```

---

## License & Credits

- **Loopcut & Blender Core**: Licensed under the **GNU General Public License v3.0 or later** ([LICENSE](LICENSE)).
- **Claude Blender Integration Handlers**: Licensed under the **MIT License** ([scripts/addons_core/loopcut/studio_vendor/LICENSE](scripts/addons_core/loopcut/studio_vendor/LICENSE)).
- **Bundled Extensions**: Shipped under their respective open-source licenses (GPL / MIT / BSD) as documented in `release/extensions/bundled.json`.

*Blender is a registered trademark of the Blender Foundation. Loopcut is an independent fork based on Blender and is not endorsed by or affiliated with the Blender Foundation.*
