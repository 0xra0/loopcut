"""Visible export copies preserve referenced assets without replacing project files."""
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

from test_storage_recovery import StorageFixture
from loopcut import files, state, workspace


class DeliverableDependenciesTest(StorageFixture):
    def test_case_variant_project_write_is_staged_without_replacing_original(self):
        ws, project = self.root / 'working', self.root / 'Project'
        ws.mkdir()
        project.mkdir()
        original = project / 'model.obj'
        original.write_text('original')
        chat = state.new_session()
        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform), \
                    patch.object(workspace, 'current', return_value=ws), patch.object(workspace, '_ROOTS', {ws}), \
                    patch.object(files, 'blend_base', return_value=(project, '')), \
                    patch.object(files, '_forbidden_roots', return_value=()), \
                    patch.object(state, 'session', return_value=chat):
                files.write_file(str(self.root / 'project' / 'model.obj'), 'replacement', overwrite=True)
                self.assertEqual(original.read_text(), 'original')
                self.assertEqual((ws / original.name).read_text(), 'replacement')
                self.assertEqual(Path(chat['items'][-1]['saved_path']).read_text(), 'replacement')

    def outputs(self, folder, suffix):
        folder.mkdir(parents=True, exist_ok=True)
        texture = folder / 'textures' / 'base color.png'
        texture.parent.mkdir(exist_ok=True)
        texture.write_bytes(b'texture')
        (folder / 'mesh.bin').write_bytes(b'buffer')
        (folder / 'unrelated.png').write_bytes(b'unrelated')
        output = folder / ('mesh' + suffix)
        text = (json.dumps({'buffers': [{'uri': 'mesh.bin'}], 'images': [{'uri': 'textures/base%20color.png'}]})
                if suffix == '.gltf' else 'newmtl Surface\nmap_Kd -s 1 1 1 textures/base color.png\n')
        output.write_text(text)
        return output, text

    def assert_delivered(self, destination, suffix):
        exported = destination / 'Export-2'
        self.assertTrue((exported / ('mesh' + suffix)).is_file())
        self.assertTrue((exported / 'textures' / 'base color.png').is_file())
        self.assertEqual((exported / 'textures' / 'base color.png').read_bytes(), b'texture')
        if suffix == '.gltf':
            self.assertEqual((exported / 'mesh.bin').read_bytes(), b'buffer')
        self.assertFalse((exported / 'unrelated.png').exists())

    def test_operator_collision_copies_unchanged_referenced_sidecars(self):
        for suffix in ('.gltf', '.mtl'):
            with self.subTest(suffix=suffix):
                source, destination = self.root / suffix[1:] / 'working', self.root / suffix[1:] / 'visible'
                output, text = self.outputs(source, suffix)
                destination.mkdir()
                (destination / output.name).write_text('existing export')
                module = types.ModuleType('bpy.ops')
                def create(*args):
                    def write(**kwargs):
                        output.write_text(text + '\n')
                        return {'FINISHED'}
                    return write
                module._op_create_function = create
                with patch.dict(sys.modules, {'bpy.ops': module}), \
                        patch.object(files, 'blend_base', return_value=(destination, '')), \
                        patch.object(state, 'session', return_value=state.new_session()):
                    with files.watching_deliverables():
                        module._op_create_function('export_scene', 'gltf')(filepath=str(output))
                self.assert_delivered(destination, suffix)
                self.assertEqual((destination / output.name).read_text(), 'existing export')

    def test_background_collision_copies_unchanged_referenced_sidecars(self):
        for suffix in ('.gltf', '.mtl'):
            with self.subTest(suffix=suffix):
                ws, destination = self.root / suffix[1:] / 'working', self.root / suffix[1:] / 'visible'
                output, text = self.outputs(ws / 'exports', suffix)
                destination.mkdir()
                (destination / output.name).write_text('existing export')
                before = workspace.files_state(ws)
                output.write_text(text + '\n')
                workspace._publish_outputs(state.new_session(), ws, before, workspace.files_state(ws), destination, lambda f: f())
                self.assert_delivered(destination, suffix)

    def test_sidecar_references_cannot_publish_files_outside_source_folder(self):
        source, destination = self.root / 'working', self.root / 'visible'
        source.mkdir()
        (self.root / 'outside.bin').write_bytes(b'outside')
        (source / 'linked.bin').symlink_to(self.root / 'outside.bin')
        (source / 'mesh.gltf').write_text(json.dumps({'buffers': [
            {'uri': '../outside.bin'}, {'uri': 'linked.bin'}, {'uri': 'https://example.invalid/file.bin'}]}))
        paths = files.publish_sources(source, ['mesh.gltf'], destination)
        self.assertEqual(paths, [destination / 'mesh.gltf'])
