# BakeNormalForAll —— 通用法线烘焙: 任意来源 → 任意网格属性(UV / 顶点色 / 通用属性),
# 支持半球 XY / 八面体压缩、RG/BA 双法线打包、轮廓宽度 Alpha 等游戏向工作流。
# 入口: View3D 侧边栏(N) > BakeNormalForAll。
bl_info = {
    "name": "Bake Normal For All",
    "author": "ShiyumeMeguri",
    "description": "通用法线烘焙: 平滑/代理/球面化法线 → UV·顶点色·任意属性,含压缩与双法线打包",
    "blender": (4, 1, 0),
    "version": (1, 1, 0),
    "location": "View3D > Sidebar(N) > BakeNormalForAll",
    "warning": "",
    "category": "Object",
}

if "bake_operator" in locals():
    import importlib
    importlib.reload(_attribute_io)
    importlib.reload(_normal_source)
    importlib.reload(_normal_encode)
    importlib.reload(properties)
    importlib.reload(bake_operator)
    importlib.reload(panel)
else:
    from . import _attribute_io, _normal_source, _normal_encode, properties, bake_operator, panel

import bpy

classes = (
    properties.SHIYUME_PG_bake_normal_for_all,
    bake_operator.SHIYUME_OT_bake_normal_to_attribute,
    panel.SHIYUME_PT_bake_normal_for_all,
    panel.SHIYUME_PT_bake_normal_dual,
    panel.SHIYUME_PT_bake_normal_target,
    panel.SHIYUME_PT_bake_normal_encoding,
    panel.SHIYUME_PT_bake_normal_alpha,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bake_normal_for_all = bpy.props.PointerProperty(
        type=properties.SHIYUME_PG_bake_normal_for_all)


def unregister():
    del bpy.types.Scene.bake_normal_for_all
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
