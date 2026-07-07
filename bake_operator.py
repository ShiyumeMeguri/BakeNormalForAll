# 法线烘焙算子 —— 读取 Scene 上的 BakeNormalForAll 设置,执行:
# 来源采集(_normal_source) → 空间变换/压缩/打包(_normal_encode) → 属性写入(_attribute_io)。
#
# 自动兜底原则: 能自动解决的问题绝不打断用户 ——
#   · N 边面: 编码层内部走临时三角副本算 MikkTSpace(见 _normal_encode);
#   · 来源物体未指定: 自动选用另一个选中的网格;
#   · 切线基准 UV 在某目标上不存在: 该物体自动回退活动 UV 层;
#   · 分量压缩与目标通道数不符: 自动适配(XYZ↔XY);
#   · 字节颜色目标: 自动强制无符号打包。
import bpy
import numpy as np

from . import _attribute_io, _normal_encode, _normal_source, properties
from ._attribute_io import BakeCancel


class _SourceSpecification:
    """一份法线来源的执行期描述(主/第二法线各一份)。"""
    __slots__ = ("mode", "source_object", "pivot_world", "blend_factor", "surface_source")

    def __init__(self):
        self.mode = 'CURRENT'
        self.source_object = None
        self.pivot_world = None
        self.blend_factor = 1.0
        self.surface_source = None  # 最近表面 BVH 采样包,跨目标物体复用


class _TargetPlan:
    """目标层的执行期描述。层创建全部提前完成,写入时按名重取引用,
    避免 CustomData 重排(创建层 / 切线计算)造成的悬空引用。"""
    __slots__ = ("kind", "primary_name", "secondary_name", "channel_count",
                 "is_byte", "attribute_existed_before")

    def __init__(self):
        self.kind = 'COLOR'
        self.primary_name = ""
        self.secondary_name = ""
        self.channel_count = 4
        self.is_byte = False
        self.attribute_existed_before = False


class SHIYUME_OT_bake_normal_to_attribute(bpy.types.Operator):
    """按侧边栏 BakeNormalForAll 面板的设置,把法线烘焙进选中网格的属性"""
    bl_idname = "shiyume.bake_normal_to_attribute"
    bl_label = "烘焙法线"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("需要在物体模式下执行")
            return False
        active_object = context.active_object
        if active_object is None or active_object.type != 'MESH':
            cls.poll_message_set("需要一个激活的网格物体")
            return False
        return True

    def execute(self, context):
        settings = context.scene.bake_normal_for_all
        notes = []
        try:
            specifications = [self._resolve_specification(context, settings, secondary=False, notes=notes)]
            if settings.dual_enable:
                specifications.append(
                    self._resolve_specification(context, settings, secondary=True, notes=notes))
        except BakeCancel as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        # 目标集合 = 选中网格(含活动物体),剔除被用作来源/枢轴的物体。
        excluded_objects = set()
        for specification in specifications:
            if specification.source_object is not None:
                excluded_objects.add(specification.source_object)
        targets = [candidate for candidate in context.selected_objects
                   if candidate.type == 'MESH' and candidate not in excluded_objects]
        active_object = context.active_object
        if (active_object is not None and active_object.type == 'MESH'
                and active_object not in excluded_objects and active_object not in targets):
            targets.append(active_object)
        if not targets:
            self.report({'ERROR'}, "没有可烘焙的目标网格(来源物体本身不作为目标)")
            return {'CANCELLED'}

        depsgraph = None
        if any(specification.mode == 'OBJECT_SURFACE' for specification in specifications):
            depsgraph = context.evaluated_depsgraph_get()

        baked_count = 0
        failures = []
        for target_object in targets:
            try:
                self._bake_object(settings, target_object, specifications, depsgraph, notes)
                baked_count += 1
            except BakeCancel as error:
                failures.append(target_object.name + ": " + str(error))

        for note in notes:
            self.report({'INFO'}, note)
        for failure in failures:
            self.report({'WARNING'}, failure)
        if baked_count == 0:
            self.report({'ERROR'}, "全部目标烘焙失败: " + "; ".join(failures))
            return {'CANCELLED'}
        self.report({'INFO'}, "法线烘焙完成: " + str(baked_count) + "/" + str(len(targets))
                    + " 个物体 → " + self._target_description(settings))
        return {'FINISHED'}

    # ------------------------------------------------------------ 来源解析

    def _resolve_specification(self, context, settings, secondary, notes):
        specification = _SourceSpecification()
        specification.mode = settings.source_mode_secondary if secondary else settings.source_mode
        specification.blend_factor = (settings.blend_factor_secondary if secondary
                                      else settings.blend_factor)
        role = "第二法线" if secondary else "法线"
        if specification.mode in {'OBJECT_TOPOLOGY', 'OBJECT_SURFACE'}:
            source_object = (settings.source_object_secondary if secondary
                             else settings.source_object)
            if source_object is None:
                source_object = self._pick_other_selected_mesh(context)
                if source_object is None:
                    raise BakeCancel(role + "来源需要一个网格物体: 请在面板指定,"
                                            "或同时选中来源与目标两个网格")
                notes.append(role + "来源自动选用 \"" + source_object.name + "\"")
            if source_object.type != 'MESH':
                raise BakeCancel(role + "来源物体 \"" + source_object.name + "\" 不是网格")
            specification.source_object = source_object
        elif specification.mode == 'PIVOT':
            pivot_object = settings.pivot_object_secondary if secondary else settings.pivot_object
            if pivot_object is not None:
                specification.pivot_world = np.array(
                    pivot_object.matrix_world.translation, dtype=np.float64)
                if pivot_object.type == 'MESH':
                    specification.source_object = pivot_object  # 枢轴网格不作为烘焙目标
            else:
                specification.pivot_world = np.array(
                    context.scene.cursor.location, dtype=np.float64)
        return specification

    @staticmethod
    def _pick_other_selected_mesh(context):
        for candidate in context.selected_objects:
            if candidate.type == 'MESH' and candidate is not context.active_object:
                return candidate
        return None

    # ------------------------------------------------------------ 逐物体烘焙

    def _bake_object(self, settings, target_object, specifications, depsgraph, notes):
        mesh = target_object.data
        if len(mesh.loops) == 0:
            raise BakeCancel("网格没有面角,无法烘焙")
        corner_vertex = _attribute_io.corner_vertex_indices(mesh)

        # 1) 记录既有活动 UV: 新建 UV 层会抢占 active,恢复用户活动层 + 切线基准锚定都靠它。
        previous_active_uv_name = ""
        if mesh.uv_layers.active is not None:
            previous_active_uv_name = mesh.uv_layers.active.name

        # 2) 解析并创建目标层(所有层创建先于任何缓存读取/切线计算)。
        plan = self._resolve_target_plan(settings, mesh)
        if plan.kind == 'UV' and previous_active_uv_name:
            previous_active_layer = mesh.uv_layers.get(previous_active_uv_name)
            if previous_active_layer is not None:
                mesh.uv_layers.active = previous_active_layer

        # 3) 分量压缩自动适配目标通道数。
        components, component_note = properties.effective_components(
            settings, plan.channel_count, len(specifications) > 1)
        if component_note is not None:
            note = target_object.name + ": " + component_note
            if note not in notes:
                notes.append(note)

        # 4) 切线基准 UV 按物体解析: 显式锚定到具体层名,绝不落在本次新建的空 UV 上。
        tangent_uv_name = settings.tangent_uv_map
        if tangent_uv_name and tangent_uv_name not in mesh.uv_layers:
            notes.append(target_object.name + ": 切线基准 UV \"" + tangent_uv_name
                         + "\" 不存在,已回退到原活动 UV 层")
            tangent_uv_name = ""
        if not tangent_uv_name:
            tangent_uv_name = previous_active_uv_name
        if settings.space == 'TANGENT' and not tangent_uv_name:
            raise BakeCancel("切线空间需要一张既有 UV 层(网格原本没有 UV);"
                             "请先展 UV 或改用物体空间")

        # 5) 采集来源 + 编码。
        positions_world = None
        if any(specification.mode in {'OBJECT_SURFACE', 'PIVOT'}
               for specification in specifications):
            positions_world = _normal_source.positions_rows_local_to_world(
                _attribute_io.vertex_positions_local(mesh), target_object.matrix_world)

        encoded_blocks = []
        for specification in specifications:
            normals_local = self._gather_source_normals(
                target_object, mesh, corner_vertex, positions_world, specification, depsgraph)
            encoded_blocks.append(self._encode_vectors(
                settings, target_object, mesh, normals_local, components, tangent_uv_name))

        # 6) 写入。
        encoded_secondary = encoded_blocks[1] if len(encoded_blocks) > 1 else None
        self._write_planned(settings, target_object, mesh, corner_vertex, plan,
                            encoded_blocks[0], encoded_secondary)
        mesh.update()

    def _resolve_target_plan(self, settings, mesh):
        plan = _TargetPlan()
        plan.kind = settings.target_kind
        plan.primary_name = properties.resolved_layer_name(settings, secondary=False)
        if plan.kind == 'UV':
            _attribute_io.ensure_uv_layer(mesh, plan.primary_name)
            if settings.dual_enable:
                plan.secondary_name = properties.resolved_layer_name(settings, secondary=True)
                if plan.secondary_name == plan.primary_name:
                    raise BakeCancel("双法线的两张 UV 层不能同名")
                _attribute_io.ensure_uv_layer(mesh, plan.secondary_name)
            plan.channel_count = 2
            return plan

        if plan.kind == 'COLOR':
            attribute, existed_before = _attribute_io.ensure_color_attribute(
                mesh, plan.primary_name, settings.color_data_type)
        else:
            attribute, existed_before = _attribute_io.ensure_generic_attribute(
                mesh, plan.primary_name, settings.attribute_data_type, settings.attribute_domain)
        _value_property, channel_count = _attribute_io.validate_attribute_target(attribute)
        plan.channel_count = channel_count
        plan.is_byte = attribute.data_type == 'BYTE_COLOR'
        plan.attribute_existed_before = existed_before
        if settings.dual_enable and channel_count != 4:
            raise BakeCancel("双法线打包需要 4 通道目标(颜色属性或 FLOAT_COLOR / BYTE_COLOR)")
        return plan

    def _gather_source_normals(self, target_object, mesh, corner_vertex, positions_world,
                               specification, depsgraph):
        """采集一份来源法线,输出目标局部空间逐角 (L,3) float32。"""
        mode = specification.mode
        if mode == 'CURRENT':
            normals = _attribute_io.corner_normals(mesh)
        elif mode == 'SMOOTH':
            normals = _attribute_io.vertex_normals(mesh)[corner_vertex]
        elif mode == 'OBJECT_TOPOLOGY':
            source_corner_normals = _normal_source.prepare_topology_source(
                specification.source_object, len(mesh.loops), len(mesh.vertices))
            normals = _normal_source.normals_rows_source_to_target(
                source_corner_normals, specification.source_object.matrix_world,
                target_object.matrix_world)
        elif mode == 'OBJECT_SURFACE':
            if specification.surface_source is None:
                specification.surface_source = _normal_source.prepare_surface_source(
                    specification.source_object, depsgraph)
            vertex_normals_world = _normal_source.sample_surface_normals_world(
                specification.surface_source, positions_world)
            normals = _normal_source.normals_rows_world_to_local(
                vertex_normals_world, target_object.matrix_world)[corner_vertex]
        elif mode == 'PIVOT':
            vertex_directions_world = _normal_source.pivot_directions_world(
                positions_world, specification.pivot_world)
            normals = _normal_source.normals_rows_world_to_local(
                vertex_directions_world, target_object.matrix_world)[corner_vertex]
        else:
            raise BakeCancel("未知的法线来源模式: " + str(mode))

        factor = specification.blend_factor
        if factor < 1.0 and mode != 'CURRENT':
            current_normals = _attribute_io.corner_normals(mesh)
            normals = _normal_source.normalize_rows(
                current_normals * np.float32(1.0 - factor) + normals * np.float32(factor))
        return np.ascontiguousarray(normals, dtype=np.float32)

    def _encode_vectors(self, settings, target_object, mesh, normals_local,
                        components, tangent_uv_name):
        """局部空间法线 → 编码空间 → 分量压缩,输出有符号 (L,2|3)。"""
        if settings.space == 'TANGENT':
            base_corner_normals = _attribute_io.corner_normals(mesh)
            vectors = _normal_encode.tangent_space_vectors(
                mesh, normals_local, base_corner_normals, tangent_uv_name)
        elif settings.space == 'OBJECT':
            vectors = normals_local.copy()
        else:
            vectors = _normal_source.normals_rows_local_to_world(
                normals_local, target_object.matrix_world)
        if settings.flip_green:
            vectors[:, 1] = -vectors[:, 1]
        return _normal_encode.compress_components(vectors, components)

    def _write_planned(self, settings, target_object, mesh, corner_vertex, plan,
                       encoded_primary, encoded_secondary):
        signed = settings.value_range == 'SIGNED' and not plan.is_byte

        if plan.kind == 'UV':
            _attribute_io.write_uv_layer(
                mesh.uv_layers[plan.primary_name],
                _normal_encode.pack_value_range(encoded_primary, signed))
            if encoded_secondary is not None:
                _attribute_io.write_uv_layer(
                    mesh.uv_layers[plan.secondary_name],
                    _normal_encode.pack_value_range(encoded_secondary, signed))
            return

        attribute = mesh.attributes.get(plan.primary_name)  # 按名重取,防悬空引用
        if attribute is None:
            raise BakeCancel("目标属性 \"" + plan.primary_name + "\" 意外丢失")
        _value_property, channel_count = _attribute_io.validate_attribute_target(attribute)

        if encoded_secondary is not None:
            corner_values = _normal_encode.pack_value_range(
                np.concatenate((encoded_primary, encoded_secondary), axis=1), signed)
        elif channel_count == 4:
            if encoded_primary.shape[1] == 3:
                normal_block = encoded_primary
            else:
                # 2 分量进 4 通道: B 通道补编码零值(无符号打包后即 0.5 中性灰)。
                normal_block = np.concatenate(
                    (encoded_primary, np.zeros((len(encoded_primary), 1), dtype=np.float32)),
                    axis=1)
            packed = _normal_encode.pack_value_range(normal_block, signed)
            alpha = self._alpha_values(settings, target_object, mesh, attribute,
                                       corner_vertex, plan.attribute_existed_before)
            corner_values = np.concatenate((packed, alpha[:, None]), axis=1)
        else:
            corner_values = _normal_encode.pack_value_range(encoded_primary, signed)

        if attribute.domain == 'POINT':
            values = _attribute_io.average_corners_to_points(
                corner_values, corner_vertex, len(mesh.vertices))
        else:
            values = corner_values
        _attribute_io.write_attribute_values(attribute, values)
        if plan.kind == 'COLOR':
            color_attribute = mesh.color_attributes.get(plan.primary_name)
            if color_attribute is not None:
                mesh.color_attributes.active_color = color_attribute  # 视口顶点色预览直接可见

    def _alpha_values(self, settings, target_object, mesh, attribute, corner_vertex,
                      existed_before):
        loop_count = len(mesh.loops)
        if settings.alpha_mode == 'VERTEX_GROUP':
            if not settings.width_vertex_group:
                raise BakeCancel("Alpha 来源设为顶点组,但未选择顶点组")
            weights = _attribute_io.vertex_group_weights(target_object, settings.width_vertex_group)
            return weights[corner_vertex]
        if settings.alpha_mode == 'KEEP' and existed_before:
            existing_values = _attribute_io.read_attribute_values(attribute)
            alpha = existing_values[:, 3]
            if attribute.domain == 'POINT':
                return np.ascontiguousarray(alpha[corner_vertex], dtype=np.float32)
            return np.ascontiguousarray(alpha, dtype=np.float32)
        return np.ones(loop_count, dtype=np.float32)

    @staticmethod
    def _target_description(settings):
        kind_labels = {'UV': "UV 层", 'COLOR': "颜色属性", 'ATTRIBUTE': "属性"}
        try:
            name = properties.resolved_layer_name(settings, secondary=False)
        except BakeCancel:
            name = "?"
        return kind_labels.get(settings.target_kind, settings.target_kind) + " \"" + name + "\""
