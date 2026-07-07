# 设置属性组 —— 挂在 Scene 上的持久化烘焙配置(N 侧边栏面板的数据源),
# 以及所有枚举定义 / 预设应用 / 目标解析辅助。
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty

from . import _attribute_io
from ._attribute_io import BakeCancel

# ---------------------------------------------------------------- 静态枚举

PRESET_ITEMS = (
    ('CUSTOM', "自定义", "手动配置全部参数"),
    ('LILTOON_OUTLINE', "lilToon 轮廓线",
     "平滑法线 → 顶点色 RGB(无符号) + Alpha 宽度,对应 lilToon 轮廓线的「RGBA → 法线 & 宽度」模式"),
    ('NPR_SMOOTH_UV', "EndField 平滑轮廓 UV",
     "平滑法线 → UV 层存切线空间 XY(有符号),引擎顶点着色器按 z=sqrt(1-x²-y²) 重建"),
    ('NPR_HAIR_DUAL', "EndField 头发双法线",
     "RG=平滑漫反射法线 XY、BA=当前高光法线 XY,无符号打包进同一层顶点色"),
)

SOURCE_MODE_ITEMS = (
    ('SMOOTH', "平滑法线", "本网格的顶点平均法线,等效「清除自定义拆边法线 + 平滑着色」,无需复制物体"),
    ('CURRENT', "当前法线", "本网格现有的角法线(含自定义拆边法线)原样使用"),
    ('OBJECT_TOPOLOGY', "同拓扑物体", "从拓扑完全一致的另一物体按角索引一一对应取法线"),
    ('OBJECT_SURFACE', "最近表面", "从任意代理网格(头皮球/简模,可带修改器)按最近表面平滑插值采样"),
    ('PIVOT', "球面化", "法线取「枢轴 → 顶点」方向;枢轴物体留空时使用 3D 游标"),
)

TARGET_KIND_ITEMS = (
    ('UV', "UV 贴图", "烘进 UV 层(float2,可存有符号值;业界惯例烘到第 2/3 张 UV)"),
    ('COLOR', "颜色属性", "烘进顶点色(传统顶点色法线管线,lilToon 等)"),
    ('ATTRIBUTE', "通用属性", "烘进任意网格属性(FLOAT_VECTOR / FLOAT2 / FLOAT_COLOR / BYTE_COLOR,角/点域)"),
)

SPACE_ITEMS = (
    ('TANGENT', "切线空间", "相对逐角 TBN 基编码(bitangent = cross(N,T) × sign),与引擎标准解码对齐"),
    ('OBJECT', "物体空间", "目标物体局部空间"),
    ('WORLD', "世界空间", "世界空间(物体再变换后即失效,仅特殊用途)"),
)

VALUE_RANGE_ITEMS = (
    ('UNSIGNED', "无符号 [0,1]", "按 v*0.5+0.5 打包;颜色/字节目标的标准形式,引擎侧 *2-1 解包"),
    ('SIGNED', "有符号 [-1,1]", "原样存储;UV/浮点目标可用,引擎侧免解包直接读"),
)

COLOR_DATA_TYPE_ITEMS = (
    ('BYTE_COLOR', "字节颜色", "8 位/通道;FBX 默认导出对字节层原样透传,顶点色法线管线首选"),
    ('FLOAT_COLOR', "浮点颜色", "32 位/通道;导出 FBX 时顶点色务必选 Linear,否则数值被 sRGB 变换扭曲"),
)

ATTRIBUTE_DATA_TYPE_ITEMS = (
    ('FLOAT_VECTOR', "3D 向量", "三分量浮点,存完整 XYZ"),
    ('FLOAT2', "2D 向量", "两分量浮点,存压缩后的 XY"),
    ('FLOAT_COLOR', "浮点颜色", "四分量浮点,可存法线 + Alpha 或双法线"),
    ('BYTE_COLOR', "字节颜色", "四分量 8 位,强制无符号打包"),
)

ATTRIBUTE_DOMAIN_ITEMS = (
    ('CORNER', "面角", "逐角存储,保留拆边差异(法线烘焙的标准域)"),
    ('POINT', "顶点", "逐顶点存储,角值按顶点平均(切线空间下为近似)"),
)

ALPHA_MODE_ITEMS = (
    ('ONE', "常量 1.0", "Alpha 通道写 1.0"),
    ('KEEP', "保留现有", "目标层已存在时保留其 Alpha 通道原值"),
    ('VERTEX_GROUP', "顶点组宽度", "用顶点组权重写入 Alpha(lilToon 轮廓宽度的笔刷工作流)"),
)

# 动态枚举 items 必须保持 Python 引用存活,否则 Blender 读到悬空字符串。
_ENUM_ITEMS_KEEPALIVE = {}


# ---------------------------------------------------------------- 动态枚举与解析

def _iter_target_layer_names(settings, context):
    active_object = context.object if context else None
    if active_object is None or active_object.type != 'MESH':
        return []
    mesh = active_object.data
    kind = settings.target_kind
    if kind == 'UV':
        return [layer.name for layer in mesh.uv_layers]
    if kind == 'COLOR':
        return [attribute.name for attribute in mesh.color_attributes]
    names = []
    for attribute in mesh.attributes:
        if attribute.name.startswith('.'):
            continue
        if attribute.data_type not in _attribute_io.VALUE_PROPERTY_BY_TYPE:
            continue
        if attribute.domain not in {'CORNER', 'POINT'}:
            continue
        names.append(attribute.name)
    return names


def _target_layer_items(settings, context):
    items = [(name, name, "") for name in _iter_target_layer_names(settings, context)]
    items.append(('__NEW__', "新建…", "创建新的目标层"))
    _ENUM_ITEMS_KEEPALIVE['target_layer'] = items
    return items


def _target_layer_secondary_items(settings, context):
    items = [(name, name, "") for name in _iter_target_layer_names(settings, context)]
    items.append(('__NEW__', "新建…", "创建新的第二目标层"))
    _ENUM_ITEMS_KEEPALIVE['target_layer_secondary'] = items
    return items


def resolved_attribute_data_type(settings, context):
    """通用属性目标的实际类型: 选中现有层用现有层的,否则用新建类型。"""
    if settings.target_layer != '__NEW__' and context is not None:
        active_object = context.object
        if active_object is not None and active_object.type == 'MESH':
            attribute = active_object.data.attributes.get(settings.target_layer)
            if attribute is not None:
                return attribute.data_type
    return settings.attribute_data_type


def resolved_target_data_type(settings, context):
    """目标层的实际数据类型(决定通道数 / 是否强制无符号)。"""
    kind = settings.target_kind
    if kind == 'UV':
        return 'FLOAT2'
    if kind == 'COLOR':
        if settings.target_layer != '__NEW__' and context is not None:
            active_object = context.object
            if active_object is not None and active_object.type == 'MESH':
                attribute = active_object.data.color_attributes.get(settings.target_layer)
                if attribute is not None:
                    return attribute.data_type
        return settings.color_data_type
    return resolved_attribute_data_type(settings, context)


def resolved_target_channel_count(settings, context):
    if settings.target_kind == 'UV':
        return 2
    data_type = resolved_target_data_type(settings, context)
    return _attribute_io.VALUE_PROPERTY_BY_TYPE.get(data_type, (None, 0))[1]


def resolved_layer_name(settings, secondary):
    """目标层名解析: 现有层用枚举值,「新建…」用名称输入框。"""
    enum_value = settings.target_layer_secondary if secondary else settings.target_layer
    if enum_value == '__NEW__':
        name = (settings.new_layer_name_secondary if secondary else settings.new_layer_name).strip()
        if not name:
            raise BakeCancel("新层名称不能为空")
        return name
    if not enum_value:
        raise BakeCancel("未选择目标层")
    return enum_value


def effective_components(settings, channel_count, dual):
    """把用户选的分量压缩自动适配到目标通道数,返回 (有效模式, 调整说明或 None)。"""
    requested = settings.components
    if dual or channel_count == 2:
        if requested in {'XY', 'OCTAHEDRAL'}:
            return requested, None
        return 'XY', "分量压缩自动调整为「半球 XY」以匹配 2 通道目标"
    if channel_count == 3:
        if requested == 'XYZ':
            return 'XYZ', None
        return 'XYZ', "分量压缩自动调整为「完整 XYZ」以匹配 3 通道目标"
    if requested in {'XYZ', 'XY', 'OCTAHEDRAL'}:
        return requested, None
    return 'XYZ', None


def _components_items(settings, context):
    """按目标通道数收窄可选压缩模式: UV/双法线/FLOAT2 只有 2 分量,FLOAT_VECTOR 只有 3 分量。"""
    force_two_components = settings.dual_enable or settings.target_kind == 'UV'
    force_three_components = False
    if not force_two_components and settings.target_kind == 'ATTRIBUTE':
        data_type = resolved_attribute_data_type(settings, context)
        if data_type == 'FLOAT2':
            force_two_components = True
        elif data_type == 'FLOAT_VECTOR':
            force_three_components = True
    items = []
    if not force_two_components:
        items.append(('XYZ', "完整 XYZ", "三分量原样存储,引擎侧无需重建"))
    if not force_three_components:
        items.append(('XY', "半球 XY", "只存切线空间 XY,引擎侧 z=sqrt(1-x²-y²) 重建(characternpr 约定)"))
        items.append(('OCTAHEDRAL', "八面体 XY", "全球域两分量压缩,引擎侧需八面体解码"))
    _ENUM_ITEMS_KEEPALIVE['components'] = items
    return items


def _apply_preset(settings, _context):
    """预设 = 一键改写其余参数;之后仍可自由微调。"""
    preset = settings.preset
    if preset == 'LILTOON_OUTLINE':
        settings.source_mode = 'SMOOTH'
        settings.dual_enable = False
        settings.target_kind = 'COLOR'
        settings.target_layer = '__NEW__'
        settings.new_layer_name = "OutlineNormal"
        settings.color_data_type = 'BYTE_COLOR'
        settings.space = 'TANGENT'
        settings.components = 'XYZ'
        settings.value_range = 'UNSIGNED'
        settings.flip_green = False
        settings.alpha_mode = 'ONE'
    elif preset == 'NPR_SMOOTH_UV':
        settings.source_mode = 'SMOOTH'
        settings.dual_enable = False
        settings.target_kind = 'UV'
        settings.target_layer = '__NEW__'
        settings.new_layer_name = "SmoothNormalTS"
        settings.space = 'TANGENT'
        settings.components = 'XY'
        settings.value_range = 'SIGNED'
        settings.flip_green = False
    elif preset == 'NPR_HAIR_DUAL':
        settings.source_mode = 'SMOOTH'
        settings.dual_enable = True
        settings.source_mode_secondary = 'CURRENT'
        settings.target_kind = 'COLOR'
        settings.target_layer = '__NEW__'
        settings.new_layer_name = "HairSplitNormal"
        settings.color_data_type = 'BYTE_COLOR'
        settings.space = 'TANGENT'
        settings.components = 'XY'
        settings.value_range = 'UNSIGNED'
        settings.flip_green = False


def _poll_mesh_object(_settings, candidate):
    return candidate.type == 'MESH'


class SHIYUME_PG_bake_normal_for_all(bpy.types.PropertyGroup):
    """BakeNormalForAll 的场景级持久化设置。"""

    preset: EnumProperty(
        name="配置预设", description="按目标管线一键配置全部参数,之后仍可自由微调",
        items=PRESET_ITEMS, default='CUSTOM', update=_apply_preset)

    # ---- 主法线来源
    source_mode: EnumProperty(
        name="法线来源", description="用哪份法线进行烘焙",
        items=SOURCE_MODE_ITEMS, default='SMOOTH')
    source_object: PointerProperty(
        name="来源物体", description="提供法线的网格物体(同拓扑 / 最近表面模式);"
                                    "留空时自动使用另一个选中的网格",
        type=bpy.types.Object, poll=_poll_mesh_object)
    pivot_object: PointerProperty(
        name="枢轴物体", description="球面化的中心物体;留空则使用 3D 游标位置",
        type=bpy.types.Object)
    blend_factor: FloatProperty(
        name="混合系数", description="来源法线与当前法线的插值比例,1 为完全使用来源法线",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR')

    # ---- 双法线(第二法线来源)
    dual_enable: BoolProperty(
        name="RG / BA 双法线", description="把两份法线的 XY 压缩打包进同一目标的 RG 与 BA 通道"
                                          "(UV 目标时写入两张 UV 层)",
        default=False)
    source_mode_secondary: EnumProperty(
        name="第二法线来源", description="BA 通道(或第二张 UV)使用的法线",
        items=SOURCE_MODE_ITEMS, default='CURRENT')
    source_object_secondary: PointerProperty(
        name="第二来源物体", description="第二法线的来源物体;留空时自动使用另一个选中的网格",
        type=bpy.types.Object, poll=_poll_mesh_object)
    pivot_object_secondary: PointerProperty(
        name="第二枢轴物体", description="第二法线球面化的中心物体;留空则使用 3D 游标位置",
        type=bpy.types.Object)
    blend_factor_secondary: FloatProperty(
        name="第二混合系数", description="第二法线与当前法线的插值比例",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR')

    # ---- 目标
    target_kind: EnumProperty(
        name="目标类型", description="法线写入哪种网格数据",
        items=TARGET_KIND_ITEMS, default='COLOR')
    target_layer: EnumProperty(
        name="目标层", description="写入的现有层,或选择「新建…」",
        items=_target_layer_items)
    target_layer_secondary: EnumProperty(
        name="第二 UV 层", description="双法线打包到 UV 时,第二法线写入的 UV 层",
        items=_target_layer_secondary_items)
    new_layer_name: StringProperty(
        name="新层名称", description="新建目标层的名字", default="BakedNormal")
    new_layer_name_secondary: StringProperty(
        name="第二新层名称", description="新建第二 UV 层的名字", default="BakedNormalB")
    color_data_type: EnumProperty(
        name="新建类型", description="新建颜色属性的数据类型",
        items=COLOR_DATA_TYPE_ITEMS, default='BYTE_COLOR')
    attribute_data_type: EnumProperty(
        name="新建类型", description="新建通用属性的数据类型",
        items=ATTRIBUTE_DATA_TYPE_ITEMS, default='FLOAT_VECTOR')
    attribute_domain: EnumProperty(
        name="新建域", description="新建通用属性的存储域",
        items=ATTRIBUTE_DOMAIN_ITEMS, default='CORNER')

    # ---- 编码
    space: EnumProperty(
        name="法线空间", description="法线编码使用的坐标空间",
        items=SPACE_ITEMS, default='TANGENT')
    tangent_uv_map: StringProperty(
        name="切线基准 UV", description="计算 TBN 使用的 UV 层;留空使用活动层"
                                       "(需与引擎计算切线所用的 UV 一致,通常是第 1 张)")
    components: EnumProperty(
        name="分量压缩", description="法线分量的压缩方式(可选项随目标通道数自动收窄,"
                                    "烘焙时也会自动适配目标通道)",
        items=_components_items)
    value_range: EnumProperty(
        name="数值范围", description="写入目标前的数值映射;字节颜色目标强制无符号",
        items=VALUE_RANGE_ITEMS, default='UNSIGNED')
    flip_green: BoolProperty(
        name="翻转 Y (绿通道)", description="取反副切线方向分量,适配 DirectX 风格法线约定的引擎",
        default=False)

    # ---- Alpha(仅单法线 + 四通道目标)
    alpha_mode: EnumProperty(
        name="Alpha 通道", description="四通道目标的 Alpha 写入策略",
        items=ALPHA_MODE_ITEMS, default='ONE')
    width_vertex_group: StringProperty(
        name="宽度顶点组", description="作为 Alpha(轮廓宽度)写入的顶点组")
