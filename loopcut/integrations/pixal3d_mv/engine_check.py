"""Real editor import checks. Missing editors are reported as not_run, never passed."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def check_engine(manifest, engine, executable=None, project=None, mode="auto"):
    manifest = Path(manifest).resolve()
    root = manifest.parent
    if mode == "off" or engine == "generic":
        return {"status": "not_run", "engine": engine, "reason": "disabled or generic preset"}
    if engine in ("cyberpunk", "witcher3"):
        return {"status": "not_run", "engine": engine,
                "reason": "Native WolvenKit/REDkit import adapter is not implemented; follow redengine/IMPORT.md and test in game"}
    command = executable or os.environ.get({"godot": "GODOT_BIN", "unity": "UNITY_BIN", "unreal": "UNREAL_BIN"}[engine])
    command = command or next((shutil.which(x) for x in {"godot": ("godot", "godot4"),
                                                       "unity": ("Unity",), "unreal": ("UnrealEditor",)}[engine]
                               if shutil.which(x)), None)
    if not command:
        return {"status": "not_run", "engine": engine, "reason": "editor executable not available"}
    if engine in ("unity", "unreal") and not project:
        return {"status": "not_run", "engine": engine, "reason": "--engine-project is required for this editor"}
    manifest_data = json.loads(manifest.read_text())
    if engine in ("unity", "unreal") and manifest_data.get("engine") != engine:
        return {"status": "failed", "engine": engine, "reason": "Rebuild with the matching engine preset before import"}
    if engine == "unity" and not (Path(project) / "ProjectSettings").is_dir():
        return {"status": "failed", "engine": engine, "reason": "Unity project directory has no ProjectSettings folder"}
    if engine == "unreal" and not Path(project).is_file():
        return {"status": "failed", "engine": engine, "reason": "Unreal .uproject file does not exist"}
    directory = root / "engine_check"
    directory.mkdir(exist_ok=True)
    result_path = directory / "result.json"
    if result_path.exists():
        result_path.unlink()
    started = time.time_ns()
    adapter = Path(__file__).resolve().parent / "engine_adapters"
    env = dict(os.environ, PA_ASSET_MANIFEST=str(manifest), PA_ASSET_CHECK_OUTPUT=str(directory / "result.json"))
    commands = []
    if engine == "godot":
        data = json.loads(manifest.read_text())
        assets = directory / "assets"
        assets.mkdir(exist_ok=True)
        for lod in data["lods"]:
            path = root / lod["files"]["glb"]
            shutil.copy2(path, assets / path.name)
        if data["collision"].get("glb"):
            path = root / data["collision"]["glb"]
            shutil.copy2(path, assets / path.name)
        (directory / "project.godot").write_text('[application]\nconfig/name="Pixal3D asset import check"\n'
                                               '[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
        shutil.copy2(adapter / "godot_import.gd", directory / "check.gd")
        (directory / "manifest.json").write_text(manifest.read_text())
        commands = [[command, "--headless", "--path", str(directory), "--import"],
                    [command, "--headless", "--path", str(directory), "--script", "check.gd"]]
    elif engine == "unity":
        editor = Path(project).resolve() / "Assets" / "Editor"
        editor.mkdir(parents=True, exist_ok=True)
        # One dedicated integration file; user project content is left intact.
        target = editor / "PixalAssetImporter.cs"
        if target.exists() and target.read_bytes() != (adapter / target.name).read_bytes():
            return {"status": "failed", "engine": engine, "reason": "existing PixalAssetImporter.cs differs; preserve it"}
        shutil.copy2(adapter / target.name, target)
        commands = [[command, "-batchmode", "-nographics", "-quit", "-projectPath", str(Path(project).resolve()),
                     "-executeMethod", "PixalAssetImporter.Run", "-logFile", str(directory / "unity.log")]]
    else:
        commands = [[command, str(Path(project).resolve()), "-unattended", "-nullrhi", "-nosplash",
                     "-run=pythonscript", "-script=" + str(adapter / "unreal_import.py")]]
    try:
        for i, args in enumerate(commands):
            result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=600,
                                    cwd=str(Path(project).resolve()) if engine == "unity" else None)
            (directory / f"editor_{i}.log").write_text(result.stdout + "\n" + result.stderr)
            if result.returncode:
                return {"status": "failed", "engine": engine, "exit_code": result.returncode,
                        "log": str(directory / f"editor_{i}.log")}
        path = directory / "result.json"
        if not path.exists() or path.stat().st_mtime_ns < started:
            return {"status": "failed", "engine": engine, "reason": "editor returned no structured import result"}
        result = json.loads(path.read_text())
        if result.get("status") != "passed" or result.get("engine") != engine:
            result["status"] = "failed"
        return result
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"status": "failed", "engine": engine, "reason": str(exc)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--engine", choices=("godot", "unity", "unreal", "cyberpunk", "witcher3"), required=True)
    parser.add_argument("--engine-bin")
    parser.add_argument("--engine-project")
    args = parser.parse_args()
    result = check_engine(args.manifest, args.engine, args.engine_bin, args.engine_project, "required")
    path = Path(args.manifest)
    data = json.loads(path.read_text())
    data["engine_check"] = result
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
