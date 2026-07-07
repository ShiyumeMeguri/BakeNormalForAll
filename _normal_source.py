# 法线来源采集 —— 五种来源统一产出"目标物体局部空间下的单位法线"。
# 除 BVH 最近点查询(mathutils 只有逐点 API)外全程 numpy 批量。
#
# 行向量法线变换约定(n 为行向量, M 为 mathutils 列向量约定的 3x3):
#   局部 → 世界: n @ M⁻¹      (列向量形式 (M⁻¹)ᵀ·n 的行向量等价)
#   世界 → 局部: n @ M        (列向量形式 Mᵀ·n 的行向量等价)
# 非均匀缩放由逆转置自动吸收,变换后统一重新归一化。
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from ._attribute_io import BakeCancel, corner_normals, vertex_positions_local

_EPSILON = 1e-8


def normalize_rows(vectors):
    """逐行归一化,零向量保持为零(避免 NaN 传播)。"""
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    np.maximum(lengths, _EPSILON, out=lengths)
    return vectors / lengths


def matrix_to_numpy3(matrix):
    """mathutils 矩阵 → (3,3) float64。"""
    return np.array(matrix.to_3x3(), dtype=np.float64)


def positions_rows_local_to_world(positions, matrix_world):
    """位置批量变换到世界空间,(N,3) float32。"""
    matrix = np.array(matrix_world, dtype=np.float64)
    return (positions.astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32)


def normals_rows_local_to_world(normals, matrix_world):
    transform = np.linalg.inv(matrix_to_numpy3(matrix_world))
    return normalize_rows((normals.astype(np.float64) @ transform).astype(np.float32))


def normals_rows_world_to_local(normals, matrix_world):
    transform = matrix_to_numpy3(matrix_world)
    return normalize_rows((normals.astype(np.float64) @ transform).astype(np.float32))


def normals_rows_source_to_target(normals, source_matrix_world, target_matrix_world):
    """源物体局部 → 目标物体局部,单次矩阵链乘。"""
    transform = np.linalg.inv(matrix_to_numpy3(source_matrix_world)) @ matrix_to_numpy3(target_matrix_world)
    return normalize_rows((normals.astype(np.float64) @ transform).astype(np.float32))


def prepare_topology_source(source_object, expected_loop_count, expected_vertex_count):
    """同拓扑来源: 校验角/顶点数一致后返回源角法线(源局部空间)。"""
    source_mesh = source_object.data
    if len(source_mesh.loops) != expected_loop_count or len(source_mesh.vertices) != expected_vertex_count:
        raise BakeCancel(
            "来源物体 \"" + source_object.name + "\" 与目标拓扑不一致 (角 "
            + str(len(source_mesh.loops)) + " vs " + str(expected_loop_count) + ", 顶点 "
            + str(len(source_mesh.vertices)) + " vs " + str(expected_vertex_count) + ")")
    return corner_normals(source_mesh)


class SurfaceSource:
    """最近表面来源的采样包: 评估后网格的世界空间三角形 + 角法线 + BVH。"""
    __slots__ = ("bvh_tree", "triangle_corner_loops", "triangle_vertex_indices",
                 "vertex_positions_world", "corner_normals_world")


def prepare_surface_source(source_object, depsgraph):
    """从评估后(带修改器)的来源物体构建 BVH 采样包,可跨多个目标物体复用。"""
    evaluated_object = source_object.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_object.to_mesh()
    try:
        evaluated_mesh.calc_loop_triangles()
        triangle_count = len(evaluated_mesh.loop_triangles)
        if triangle_count == 0:
            raise BakeCancel("来源物体 \"" + source_object.name + "\" 没有可采样的三角形")

        triangle_corner_loops = np.empty(triangle_count * 3, dtype=np.int32)
        evaluated_mesh.loop_triangles.foreach_get("loops", triangle_corner_loops)
        triangle_vertex_indices = np.empty(triangle_count * 3, dtype=np.int32)
        evaluated_mesh.loop_triangles.foreach_get("vertices", triangle_vertex_indices)

        source = SurfaceSource()
        source.triangle_corner_loops = triangle_corner_loops.reshape(-1, 3)
        source.triangle_vertex_indices = triangle_vertex_indices.reshape(-1, 3)
        source.vertex_positions_world = positions_rows_local_to_world(
            vertex_positions_local(evaluated_mesh), source_object.matrix_world)
        source.corner_normals_world = normals_rows_local_to_world(
            corner_normals(evaluated_mesh), source_object.matrix_world)
        source.bvh_tree = BVHTree.FromPolygons(
            source.vertex_positions_world.tolist(),
            source.triangle_vertex_indices.tolist(),
            all_triangles=True)
        return source
    finally:
        evaluated_object.to_mesh_clear()


def sample_surface_normals_world(surface_source, query_positions_world):
    """逐查询点取最近表面的平滑法线(命中三角形的角法线重心插值),(N,3) 世界空间。"""
    query_count = len(query_positions_world)
    hit_positions = np.empty((query_count, 3), dtype=np.float32)
    hit_triangles = np.zeros(query_count, dtype=np.int64)

    # BVH 查询是整条管线里唯一无法向量化的段: 逐"顶点"(而非逐角)查询,角靠索引散射复用。
    find_nearest = surface_source.bvh_tree.find_nearest
    for index, coordinate in enumerate(query_positions_world.tolist()):
        location, _normal, triangle_index, _distance = find_nearest(Vector(coordinate))
        if location is None:
            hit_positions[index] = coordinate
        else:
            hit_positions[index] = location
            hit_triangles[index] = triangle_index

    corner_loops = surface_source.triangle_corner_loops[hit_triangles]
    triangle_vertices = surface_source.triangle_vertex_indices[hit_triangles]
    position_a = surface_source.vertex_positions_world[triangle_vertices[:, 0]].astype(np.float64)
    position_b = surface_source.vertex_positions_world[triangle_vertices[:, 1]].astype(np.float64)
    position_c = surface_source.vertex_positions_world[triangle_vertices[:, 2]].astype(np.float64)

    # 向量化重心坐标: 在命中三角形内插值三个角法线,得到平滑(而非面片)法线。
    edge_ab = position_b - position_a
    edge_ac = position_c - position_a
    offset = hit_positions.astype(np.float64) - position_a
    dot_abab = np.einsum('ij,ij->i', edge_ab, edge_ab)
    dot_abac = np.einsum('ij,ij->i', edge_ab, edge_ac)
    dot_acac = np.einsum('ij,ij->i', edge_ac, edge_ac)
    dot_oab = np.einsum('ij,ij->i', offset, edge_ab)
    dot_oac = np.einsum('ij,ij->i', offset, edge_ac)
    denominator = dot_abab * dot_acac - dot_abac * dot_abac
    degenerate = np.abs(denominator) < 1e-20
    denominator[degenerate] = 1.0
    weight_b = (dot_acac * dot_oab - dot_abac * dot_oac) / denominator
    weight_c = (dot_abab * dot_oac - dot_abac * dot_oab) / denominator
    weight_b[degenerate] = 0.0
    weight_c[degenerate] = 0.0
    weight_a = 1.0 - weight_b - weight_c

    normals = (weight_a[:, None] * surface_source.corner_normals_world[corner_loops[:, 0]]
               + weight_b[:, None] * surface_source.corner_normals_world[corner_loops[:, 1]]
               + weight_c[:, None] * surface_source.corner_normals_world[corner_loops[:, 2]])
    return normalize_rows(normals.astype(np.float32))


def pivot_directions_world(vertex_positions_world, pivot_position_world):
    """球面化来源: 从枢轴指向各顶点的单位方向,(V,3) 世界空间。"""
    directions = vertex_positions_world.astype(np.float64) - pivot_position_world[None, :]
    return normalize_rows(directions.astype(np.float32))
