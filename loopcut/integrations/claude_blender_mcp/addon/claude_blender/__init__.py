"""Claude Blender Integration: the Blender side of the MCP bridge."""

bl_info = {
    "name": "Claude Blender Integration",
    "author": "Rotem Babani",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Claude",
    "description": "Bridge that lets Claude drive Blender through MCP",
    "category": "Development",
}

import bpy

from . import protocol, server


def _prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


class CLAUDE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    port: bpy.props.IntProperty(name="Port", default=protocol.DEFAULT_PORT, min=1024, max=65535)
    autostart: bpy.props.BoolProperty(
        name="Start bridge when Blender opens",
        description="Start the local server automatically on startup",
        default=False,
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "port")
        col.prop(self, "autostart")
        col.label(text="The server only listens on 127.0.0.1.")


class CLAUDE_OT_start(bpy.types.Operator):
    bl_idname = "claude.start_bridge"
    bl_label = "Start Claude Bridge"
    bl_description = "Start the local server the Claude MCP server connects to"

    def execute(self, context):
        prefs = _prefs()
        port = prefs.port if prefs else protocol.DEFAULT_PORT
        try:
            server.start(port=port)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not listen on port {port}: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Claude bridge listening on 127.0.0.1:{port}")
        return {"FINISHED"}


class CLAUDE_OT_stop(bpy.types.Operator):
    bl_idname = "claude.stop_bridge"
    bl_label = "Stop Claude Bridge"

    def execute(self, context):
        server.stop()
        return {"FINISHED"}


class CLAUDE_PT_panel(bpy.types.Panel):
    bl_label = "Claude"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"

    def draw(self, context):
        layout = self.layout
        srv = server.get_server()
        if srv and srv.running:
            layout.label(text=f"Listening on port {srv.port}", icon="CHECKMARK")
            layout.label(text=f"Connected clients: {srv.clients}")
            layout.operator(CLAUDE_OT_stop.bl_idname, icon="PAUSE")
        else:
            layout.label(text="Bridge stopped", icon="X")
            layout.operator(CLAUDE_OT_start.bl_idname, icon="PLAY")


CLASSES = (CLAUDE_AddonPreferences, CLAUDE_OT_start, CLAUDE_OT_stop, CLAUDE_PT_panel)


def _autostart():
    prefs = _prefs()
    if prefs and prefs.autostart:
        try:
            server.start(port=prefs.port)
        except OSError as exc:
            print(f"[claude_blender] autostart failed: {exc}")
    return None


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.app.timers.register(_autostart, first_interval=1.0)


def unregister():
    server.stop()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
