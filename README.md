# Bake Normal For All

通用法线烘焙 Blender 插件(4.1+ / 5.x):把任意来源的法线,以任意编码,烘进任意网格数据。
作者: ShiyumeMeguri

入口: **View3D 侧边栏(N)> BakeNormalForAll 标签页**。设置随场景持久化,顶部一键烘焙;
分区子面板: 双法线(标题复选框开关)/ 烘焙目标 / 编码 / Alpha(仅四通道单法线时出现)。

## 能力矩阵

| 维度 | 选项 |
|---|---|
| 法线来源 | 平滑自身(免复制物体) / 当前法线 / 同拓扑物体 / 最近表面(BVH 代理采样,可带修改器) / 球面化(枢轴或 3D 游标) |
| 烘焙目标 | UV 层 / 颜色属性(字节·浮点) / 通用属性(FLOAT_VECTOR·FLOAT2·FLOAT_COLOR·BYTE_COLOR,角/点域) |
| 法线空间 | 切线空间(TBN 可指定基准 UV) / 物体空间 / 世界空间 |
| 分量压缩 | 完整 XYZ / 半球 XY(丢 Z,shader 重建) / 八面体 XY(全球域) |
| 数值范围 | 有符号 [-1,1](UV/浮点) / 无符号 [0,1](*0.5+0.5;字节目标强制) |
| 双法线 | RG/BA 打包进一层顶点色,或写两张 UV 层 |
| Alpha | 常量 1 / 保留现有 / 顶点组权重(轮廓宽度笔刷工作流) |
| 其它 | 混合系数(与当前法线插值)、翻转绿通道、多选物体批量烘焙 |

## 自动兜底(无需手动处理)

- **N 边面**: 无需三角化。引擎管线对 N 边面本来就是「先三角化再算 MikkTSpace」,插件在内部
  用临时三角副本(继承原 UV 与拆边法线)计算切线并映射回原网格,与引擎行为一致。
- **来源物体未指定**: 自动选用另一个选中的网格(信息提示选了谁)。
- **切线基准 UV 在某个目标上不存在**: 该物体自动回退活动 UV 层。
- **分量压缩与目标通道数不符**: 自动适配(2 通道目标自动用半球 XY,3 通道自动用完整 XYZ)。
- **字节颜色目标**: 自动强制无符号打包(UI 中范围项同时置灰)。

## 预设

- **lilToon 轮廓线**: 平滑法线 → 顶点色 RGB(无符号)+ Alpha=1。Unity 侧 lilToon 轮廓线设为
  「RGBA → 法线 & 宽度」;宽度可改用「顶点组宽度」写 Alpha。
- **EndField 平滑轮廓 UV**: 平滑法线 → UV 层存切线空间 XY(有符号)。引擎顶点着色器解码:

  ```hlsl
  float2 ts = input.smoothNormalTS;                       // 直接读 UV,无需 *2-1
  float z = sqrt(max(1.0 - dot(ts, ts), 0.0));            // 半球重建
  float3 bitangentWS = cross(normalWS, tangentWS.xyz) * tangentOS.w;
  float3 outlineNormalWS = normalize(tangentWS.xyz * ts.x + bitangentWS * ts.y + normalWS * z);
  ```

  Blender 第 N 张 UV 导出 FBX 后对应 Unity 的 `TEXCOORD(N-1)`;shader 若从 `TEXCOORD2` 读,
  就烘到第 3 张 UV。
- **EndField 头发双法线**: RG = 平滑漫反射法线 XY、BA = 当前高光法线 XY,无符号打包。引擎侧解码:

  ```hlsl
  float2 diffuseXY = sample.xy * 2.0 - 1.0;               // RG
  float2 specularXY = sample.zw * 2.0 - 1.0;              // BA
  // 各自 z = max(sqrt(1 - saturate(dot(xy, xy))), 1e-16) 重建后过 TBN
  ```

## 生产注意

- **顶点色走字节颜色**: FBX 默认导出(sRGB 模式)对字节层原样透传数值;浮点颜色层导出时
  必须把顶点色选项改为 Linear,否则法线数据被 sRGB 曲线扭曲。插件写字节层用原始字节视图,
  写入值 = 引擎读到的值。
- **切线空间需要 UV**(引擎侧同样如此);「切线 UV」要与引擎计算切线用的 UV 一致(通常第 1 张)。
- **半球 XY** 假设法线不偏离基法线超过 90°(轮廓平滑法线天然满足);需要全球域时用八面体。
- 来源为「最近表面」时,代理网格取修改器求值后的结果,BVH 对每个来源只构建一次,多目标复用。
