# 属性 IO —— UV 层 / 颜色属性 / 通用属性的批量读写。
# 全部走 foreach_get / foreach_set + numpy,禁止逐元素 Python 循环;
# 唯一的例外是顶点组权重(MDeformVert 没有 foreach 通道)。
import numpy as np


class BakeCancel(RuntimeError):
    """烘焙无法继续时抛出,由算子层转成 report 错误信息。"""


# 支持的目标属性类型 → (值属性名, 通道数)。
# BYTE_COLOR 必须走 color_srgb 原始字节视图:颜色属性的 color 访问器带 线性↔sRGB 变换,
# 法线是数据不是颜色,走 color 会被 gamma 扭曲(FBX 默认 sRGB 导出对字节层是原样透传,
# 所以原始字节视图写入 = 引擎侧拿到的就是写入值)。
VALUE_PROPERTY_BY_TYPE = {
    'FLOAT_VECTOR': ("vector", 3),
    'FLOAT2': ("vector", 2),
    'FLOAT_COLOR': ("color", 4),
    'BYTE_COLOR': ("color_srgb", 4),
}


def corner_vertex_indices(mesh):
    """每个 loop(角)对应的顶点索引,(L,) int32。"""
    count = len(mesh.loops)
    indices = np.empty(count, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", indices)
    return indices


def vertex_positions_local(mesh):
    """顶点局部坐标,(V,3) float32。"""
    count = len(mesh.vertices)
    positions = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    return positions.reshape(count, 3)


def corner_normals(mesh):
    """角法线(含自定义拆边法线),(L,3) float32。"""
    count = len(mesh.loops)
    normals = np.empty(count * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", normals)
    return normals.reshape(count, 3)


def vertex_normals(mesh):
    """顶点法线(= 清除自定义法线 + 平滑着色的结果),(V,3) float32。"""
    count = len(mesh.vertices)
    normals = np.empty(count * 3, dtype=np.float32)
    mesh.vertex_normals.foreach_get("vector", normals)
    return normals.reshape(count, 3)


def ensure_uv_layer(mesh, name):
    """按名取 UV 层,不存在则创建;超过 UV 层上限时报错。"""
    layer = mesh.uv_layers.get(name)
    if layer is None:
        layer = mesh.uv_layers.new(name=name, do_init=False)
        if layer is None:
            raise BakeCancel("UV 层已达上限,无法创建 \"" + name + "\"")
    return layer


def write_uv_layer(layer, values):
    """写 UV 层,values 形状 (L,2)。"""
    layer.uv.foreach_set("vector", np.ascontiguousarray(values, dtype=np.float32).ravel())


def read_uv_layer(layer):
    """读 UV 层,(L,2) float32。"""
    count = len(layer.uv)
    values = np.empty(count * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", values)
    return values.reshape(count, 2)


def ensure_color_attribute(mesh, name, data_type):
    """按名取颜色属性,不存在则按 data_type 新建角域颜色属性。返回 (属性, 是否原已存在)。"""
    attribute = mesh.color_attributes.get(name)
    if attribute is not None:
        return attribute, True
    attribute = mesh.color_attributes.new(name=name, type=data_type, domain='CORNER')
    if attribute is None:
        raise BakeCancel("无法创建颜色属性 \"" + name + "\"")
    return attribute, False


def ensure_generic_attribute(mesh, name, data_type, domain):
    """按名取通用属性,不存在则新建。返回 (属性, 是否原已存在)。"""
    attribute = mesh.attributes.get(name)
    if attribute is not None:
        return attribute, True
    attribute = mesh.attributes.new(name=name, type=data_type, domain=domain)
    if attribute is None:
        raise BakeCancel("无法创建属性 \"" + name + "\"")
    return attribute, False


def validate_attribute_target(attribute):
    """校验属性可作为烘焙目标,返回 (值属性名, 通道数)。"""
    entry = VALUE_PROPERTY_BY_TYPE.get(attribute.data_type)
    if entry is None:
        raise BakeCancel(
            "属性 \"" + attribute.name + "\" 的类型 " + attribute.data_type +
            " 不支持烘焙(支持 FLOAT_VECTOR / FLOAT2 / FLOAT_COLOR / BYTE_COLOR)")
    if attribute.domain not in {'CORNER', 'POINT'}:
        raise BakeCancel("属性 \"" + attribute.name + "\" 的域 " + attribute.domain + " 不支持烘焙(仅角/点域)")
    return entry


def read_attribute_values(attribute):
    """整层读出属性值,(N, 通道数) float32。BYTE_COLOR 读原始字节视图。"""
    value_property, channel_count = validate_attribute_target(attribute)
    count = len(attribute.data)
    values = np.empty(count * channel_count, dtype=np.float32)
    attribute.data.foreach_get(value_property, values)
    return values.reshape(count, channel_count)


def write_attribute_values(attribute, values):
    """整层写入属性值,values 形状 (N, 通道数)。"""
    value_property, channel_count = validate_attribute_target(attribute)
    if values.shape[1] != channel_count:
        raise BakeCancel(
            "内部通道数不匹配: 目标 " + str(channel_count) + " 通道,得到 " + str(values.shape[1]) + " 通道")
    attribute.data.foreach_set(value_property, np.ascontiguousarray(values, dtype=np.float32).ravel())


def average_corners_to_points(corner_values, corner_vertex, vertex_count):
    """角域值按顶点平均,得到点域值 (V,C)。float64 累加避免大网格精度塌陷。"""
    channel_count = corner_values.shape[1]
    accumulator = np.zeros((vertex_count, channel_count), dtype=np.float64)
    np.add.at(accumulator, corner_vertex, corner_values.astype(np.float64))
    counts = np.bincount(corner_vertex, minlength=vertex_count).astype(np.float64)
    counts[counts == 0.0] = 1.0
    return (accumulator / counts[:, None]).astype(np.float32)


def vertex_group_weights(target_object, group_name):
    """顶点组权重,(V,) float32,未指派顶点为 0。"""
    group = target_object.vertex_groups.get(group_name)
    if group is None:
        raise BakeCancel("物体 \"" + target_object.name + "\" 没有顶点组 \"" + group_name + "\"")
    mesh = target_object.data
    weights = np.zeros(len(mesh.vertices), dtype=np.float32)
    group_index = group.index
    # MDeformVert 没有 foreach 批量通道,这里是属性 IO 中唯一的逐元素循环。
    for vertex in mesh.vertices:
        for element in vertex.groups:
            if element.group == group_index:
                weights[vertex.index] = element.weight
                break
    return weights
