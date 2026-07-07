# N 侧边栏面板 —— View3D > 侧边栏 > BakeNormalForAll。
# 官方式布局: 根面板放烘焙按钮 + 预设 + 来源,子面板分「双法线 / 目标 / 编码 / Alpha」,
# 双法线子面板用标题复选框开关,Alpha 子面板仅在目标为四通道且非双法线时出现。
import bpy

from . import properties


def _draw_source_settings(layout, settings, secondary):
    """来源设置的公共绘制(主法线 / 第二法线共用)。"""
    mode = settings.source_mode_secondary if secondary else settings.source_mode
    if mode in {'OBJECT_TOPOLOGY', 'OBJECT_SURFACE'}:
        layout.prop(settings, "source_object_secondary" if secondary else "source_object",
                    text="来源物体")
    elif mode == 'PIVOT':
        layout.prop(settings, "pivot_object_secondary" if secondary else "pivot_object",
                    text="枢轴物体")
    layout.prop(settings, "blend_factor_secondary" if secondary else "blend_factor",
                text="混合系数", slider=True)


class SHIYUME_PT_bake_normal_for_all(bpy.types.Panel):
    bl_idname = "SHIYUME_PT_bake_normal_for_all"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BakeNormalForAll"
    bl_label = "Bake Normal For All"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bake_normal_for_all
        layout.use_property_split = True
        layout.use_property_decorate = False

        bake_column = layout.column()
        bake_column.scale_y = 1.4
        bake_column.operator("shiyume.bake_normal_to_attribute",
                             text="烘焙法线", icon='NORMALS_VERTEX_FACE')

        layout.prop(settings, "preset")

        source_column = layout.column(heading="法线来源")
        source_column.prop(settings, "source_mode", text="来源")
        _draw_source_settings(source_column, settings, secondary=False)


class SHIYUME_PT_bake_normal_dual(bpy.types.Panel):
    bl_idname = "SHIYUME_PT_bake_normal_dual"
    bl_parent_id = "SHIYUME_PT_bake_normal_for_all"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BakeNormalForAll"
    bl_label = "双法线 (RG / BA)"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        settings = context.scene.bake_normal_for_all
        self.layout.prop(settings, "dual_enable", text="")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bake_normal_for_all
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.enabled = settings.dual_enable

        column = layout.column(heading="第二法线")
        column.prop(settings, "source_mode_secondary", text="来源")
        _draw_source_settings(column, settings, secondary=True)


class SHIYUME_PT_bake_normal_target(bpy.types.Panel):
    bl_idname = "SHIYUME_PT_bake_normal_target"
    bl_parent_id = "SHIYUME_PT_bake_normal_for_all"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BakeNormalForAll"
    bl_label = "烘焙目标"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bake_normal_for_all
        layout.use_property_split = True
        layout.use_property_decorate = False

        column = layout.column()
        column.prop(settings, "target_kind", text="类型")
        column.prop(settings, "target_layer", text="目标层")
        if settings.target_layer == '__NEW__':
            column.prop(settings, "new_layer_name", text="名称")
            if settings.target_kind == 'COLOR':
                column.prop(settings, "color_data_type", text="类型")
            elif settings.target_kind == 'ATTRIBUTE':
                column.prop(settings, "attribute_data_type", text="类型")
                column.prop(settings, "attribute_domain", text="域")
        if settings.dual_enable and settings.target_kind == 'UV':
            column.separator()
            column.prop(settings, "target_layer_secondary", text="第二 UV 层")
            if settings.target_layer_secondary == '__NEW__':
                column.prop(settings, "new_layer_name_secondary", text="第二名称")
        if (settings.dual_enable and settings.target_kind != 'UV'
                and properties.resolved_target_channel_count(settings, context) != 4):
            column.label(text="双法线需要 4 通道目标", icon='ERROR')


class SHIYUME_PT_bake_normal_encoding(bpy.types.Panel):
    bl_idname = "SHIYUME_PT_bake_normal_encoding"
    bl_parent_id = "SHIYUME_PT_bake_normal_for_all"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BakeNormalForAll"
    bl_label = "编码"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bake_normal_for_all
        layout.use_property_split = True
        layout.use_property_decorate = False

        column = layout.column()
        column.prop(settings, "space", text="空间")
        if settings.space == 'TANGENT':
            active_object = context.object
            if active_object is not None and active_object.type == 'MESH':
                column.prop_search(settings, "tangent_uv_map",
                                   active_object.data, "uv_layers", text="切线 UV")
        column.prop(settings, "components", text="压缩")
        range_row = column.row()
        range_row.enabled = properties.resolved_target_data_type(settings, context) != 'BYTE_COLOR'
        range_row.prop(settings, "value_range", text="范围")
        column.prop(settings, "flip_green")


class SHIYUME_PT_bake_normal_alpha(bpy.types.Panel):
    bl_idname = "SHIYUME_PT_bake_normal_alpha"
    bl_parent_id = "SHIYUME_PT_bake_normal_for_all"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BakeNormalForAll"
    bl_label = "Alpha"

    @classmethod
    def poll(cls, context):
        settings = context.scene.bake_normal_for_all
        if settings.dual_enable:
            return False
        return properties.resolved_target_channel_count(settings, context) == 4

    def draw(self, context):
        layout = self.layout
        settings = context.scene.bake_normal_for_all
        layout.use_property_split = True
        layout.use_property_decorate = False

        column = layout.column()
        column.prop(settings, "alpha_mode", text="写入")
        if settings.alpha_mode == 'VERTEX_GROUP':
            active_object = context.object
            if active_object is not None:
                column.prop_search(settings, "width_vertex_group",
                                   active_object, "vertex_groups", text="顶点组")
