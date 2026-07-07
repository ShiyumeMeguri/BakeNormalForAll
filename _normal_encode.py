# 法线编码 —— 切线空间变换、分量压缩、数值范围打包。全 numpy 批量。
#
# 切线空间约定与引擎侧解码严格对齐(characternpr 家族):
#   bitangent = cross(N, T) * bitangent_sign
#   x = n·T, y = n·B, z = n·N;半球压缩时 shader 里 z = sqrt(max(1 - x² - y², 0)) 重建。
# 八面体压缩为全球域备选(引擎解码需对应 octahedral decode)。
#
# N 边面自动处理: calc_tangents(MikkTSpace)只接受三角/四边面,而引擎管线对 N 边面网格
# 本来就是「先三角化再算 MikkTSpace」。因此含 N 边面时在内部用 loop_triangles 构建临时
# 三角副本(继承原 UV + 原角法线)计算切线,再按角索引映射回原网格 —— 与引擎行为一致,
# 用户无需手动三角化。
import bpy
import numpy as np

from ._attribute_io import BakeCancel, corner_normals, read_uv_layer, vertex_positions_local

COMPONENTS_XYZ = 'XYZ'
COMPONENTS_HEMISPHERE_XY = 'XY'
COMPONENTS_OCTAHEDRAL = 'OCTAHEDRAL'


def _mesh_has_ngons(mesh):
    polygon_count = len(mesh.polygons)
    if polygon_count == 0:
        return False
    loop_totals = np.empty(polygon_count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", loop_totals)
    return bool((loop_totals > 4).any())


def _corner_tangents_direct(mesh, tangent_uv_name):
    """三角/四边网格的快路径: 直接在原网格上算 MikkTSpace 切线。"""
    mesh.calc_tangents(uvmap=tangent_uv_name)
    loop_count = len(mesh.loops)
    tangents = np.empty(loop_count * 3, dtype=np.float32)
    mesh.loops.foreach_get("tangent", tangents)
    bitangent_signs = np.empty(loop_count, dtype=np.float32)
    mesh.loops.foreach_get("bitangent_sign", bitangent_signs)
    mesh.free_tangents()
    return tangents.reshape(loop_count, 3), bitangent_signs


def _corner_tangents_via_triangulated_copy(mesh, tangent_uv_name):
    """N 边面网格: 在临时三角副本上算 MikkTSpace,按原角索引映射回来。

    loop_triangles 的每个三角角都直接引用原网格 loop 索引,且任意三角化必覆盖多边形的
    全部顶点,所以每个原角至少出现一次 —— 映射无空洞;同角多次出现取最后一次,确定性。
    """
    mesh.calc_loop_triangles()
    triangle_count = len(mesh.loop_triangles)
    if triangle_count == 0:
        raise BakeCancel("网格没有可三角化的面,无法计算切线")
    triangle_loops = np.empty(triangle_count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", triangle_loops)

    loop_count = len(mesh.loops)
    corner_vertex = np.empty(loop_count, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", corner_vertex)

    if tangent_uv_name:
        uv_layer = mesh.uv_layers[tangent_uv_name]
    else:
        uv_layer = mesh.uv_layers.active or mesh.uv_layers[0]
    original_uv = read_uv_layer(uv_layer)
    original_normals = corner_normals(mesh)
    positions = vertex_positions_local(mesh)

    temporary_mesh = bpy.data.meshes.new("BakeNormalForAll_TangentTemporary")
    try:
        temporary_mesh.vertices.add(len(mesh.vertices))
        temporary_mesh.vertices.foreach_set("co", positions.ravel())
        temporary_mesh.loops.add(triangle_count * 3)
        temporary_mesh.loops.foreach_set("vertex_index", corner_vertex[triangle_loops])
        temporary_mesh.polygons.add(triangle_count)
        temporary_mesh.polygons.foreach_set(
            "loop_start", np.arange(0, triangle_count * 3, 3, dtype=np.int32))
        temporary_mesh.update(calc_edges=True)

        temporary_uv_layer = temporary_mesh.uv_layers.new(name="UVMap", do_init=False)
        if temporary_uv_layer is None:
            raise BakeCancel("临时切线网格创建 UV 层失败")
        temporary_uv_layer.uv.foreach_set(
            "vector", np.ascontiguousarray(original_uv[triangle_loops]).ravel())
        # MikkTSpace 会对法线正交化,必须继承原网格的拆边角法线,否则切线错位。
        temporary_mesh.normals_split_custom_set(original_normals[triangle_loops])

        temporary_mesh.calc_tangents(uvmap="UVMap")
        triangle_tangents = np.empty(triangle_count * 9, dtype=np.float32)
        temporary_mesh.loops.foreach_get("tangent", triangle_tangents)
        triangle_signs = np.empty(triangle_count * 3, dtype=np.float32)
        temporary_mesh.loops.foreach_get("bitangent_sign", triangle_signs)

        tangents = np.zeros((loop_count, 3), dtype=np.float32)
        tangents[:, 0] = 1.0  # 理论上无空洞,兜底为单位 X 防 NaN
        bitangent_signs = np.ones(loop_count, dtype=np.float32)
        tangents[triangle_loops] = triangle_tangents.reshape(triangle_count * 3, 3)
        bitangent_signs[triangle_loops] = triangle_signs
        return tangents, bitangent_signs
    finally:
        bpy.data.meshes.remove(temporary_mesh)


def tangent_space_vectors(mesh, source_normals, base_corner_normals, tangent_uv_name):
    """把局部空间法线批量转到逐角切线空间,(L,3)。

    base_corner_normals 必须是网格自身的角法线(含自定义拆边法线)——引擎导入后的
    顶点法线就是它,TBN 基必须与引擎一致,否则解码错位。
    """
    if len(mesh.uv_layers) == 0:
        raise BakeCancel("网格没有 UV 层,切线空间未定义;请先展 UV 或改用物体空间")
    if tangent_uv_name and tangent_uv_name not in mesh.uv_layers:
        raise BakeCancel("切线基准 UV \"" + tangent_uv_name + "\" 不存在")

    if _mesh_has_ngons(mesh):
        tangents, bitangent_signs = _corner_tangents_via_triangulated_copy(mesh, tangent_uv_name)
    else:
        try:
            tangents, bitangent_signs = _corner_tangents_direct(mesh, tangent_uv_name)
        except RuntimeError:
            # 极端兜底: 直算失败(如隐藏的退化面)时走三角副本路径。
            tangents, bitangent_signs = _corner_tangents_via_triangulated_copy(mesh, tangent_uv_name)

    bitangents = np.cross(base_corner_normals, tangents) * bitangent_signs[:, None]
    x = np.einsum('ij,ij->i', source_normals, tangents)
    y = np.einsum('ij,ij->i', source_normals, bitangents)
    z = np.einsum('ij,ij->i', source_normals, base_corner_normals)
    return np.stack((x, y, z), axis=1)


def octahedral_encode(normals):
    """八面体编码 (N,3) 单位法线 → (N,2),全球域无损方向压缩。"""
    normals = normals.astype(np.float64)
    denominator = np.abs(normals).sum(axis=1, keepdims=True)
    np.maximum(denominator, 1e-12, out=denominator)
    projected = normals[:, :2] / denominator
    negative_z = normals[:, 2] < 0.0
    if negative_z.any():
        folded = projected[negative_z]
        # 下半球折叠: (1 - |p.yx|) * sign(p.xy),sign 取原分量符号(0 视作正)。
        signs = np.where(folded >= 0.0, 1.0, -1.0)
        projected[negative_z] = (1.0 - np.abs(folded[:, ::-1])) * signs
    return projected.astype(np.float32)


def compress_components(vectors, components_mode):
    """(N,3) 有符号法线 → 按压缩模式输出 (N,3) 或 (N,2)。"""
    if components_mode == COMPONENTS_XYZ:
        return vectors
    if components_mode == COMPONENTS_HEMISPHERE_XY:
        # 半球压缩: 直接丢 Z,解码端 sqrt 重建。Z<0(偏离基法线超 90°)会被引擎钳到半球面。
        return vectors[:, :2]
    if components_mode == COMPONENTS_OCTAHEDRAL:
        return octahedral_encode(vectors)
    raise BakeCancel("未知的分量压缩模式: " + str(components_mode))


def pack_value_range(values, signed):
    """范围打包: 有符号原样输出;无符号映射 v*0.5+0.5 并钳到 [0,1](字节层量化安全)。"""
    if signed:
        return values.astype(np.float32)
    packed = values.astype(np.float32) * np.float32(0.5) + np.float32(0.5)
    return np.clip(packed, 0.0, 1.0)
