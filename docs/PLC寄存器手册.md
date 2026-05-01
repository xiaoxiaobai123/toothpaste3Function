# PLC 寄存器使用手册(中文版)

视觉系统通过 Modbus TCP 与 PLC 通讯,**视觉机做客户端**,**PLC 做服务端**(默认端口 502)。所有寄存器都是 16 位保持寄存器(holding register),地址用 `D<n>` 表示,即 Modbus address `<n>` 上的 16-bit 字。

> 英文技术参考见 [`PLC_REGISTERS.md`](PLC_REGISTERS.md)。本手册面向 PLC 工程师,**直接照着写梯形图**就行。

---

## 1. 完整寄存器地址表(速查)

### 系统级寄存器

| D 地址 | 方向 | 名称 | 类型 | 用途 |
|:-:|:-:|:--|:-:|:--|
| `D50` | PLC 写 | `plc_heartbeat` | uint16 | PLC 自己每秒翻转,视觉机只读做诊断(可不用) |
| `D120` | 视觉写 | `system_status` | uint16 | 视觉系统状态:0=启动中, 1=空闲, 2=处理中, 3=错误 |
| `D121` | 视觉写 | `error_code` | uint16 | 视觉系统错误码(异常时填) |
| `D122` | 视觉写 | `system_heartbeat` | uint16 | 视觉机每秒翻转 0↔1,**PLC 监视这位证明视觉程序还活着** |
| `D123` | 视觉写 | `cam1_status` | uint16 | 写入 `CameraStatus` 值,见下表 |
| `D124` | 视觉写 | `cam2_status` | uint16 | 同上(注:此寄存器为带符号 int16) |

### 相机配置块(PLC 写,视觉读)

PLC 把每台相机的配置参数填到这块,视觉机**每个采集循环**读一次。

| D 地址 (Cam1) | D 地址 (Cam2) | 字段 | 类型 |
|:-:|:-:|:--|:-:|
| `D1` | `D2` | `status` 触发命令 | uint16 |
| `D10` | `D30` | `trigger_mode` 触发模式 | uint16 |
| `D11` | `D31` | `exposure_time` 曝光时间(微秒) | uint16 |
| `D12-D13` | `D32-D33` | `pixel_distance` 像素物理距离 | float32 (LE) |
| `D14` | `D34` | `product_type` 产品类型(算法选择) | uint16 |
| `D15..D27` | `D35..D47` | **算法专属参数**(13 个字) | 见第 3 节 |

### 结果块(视觉写,PLC 读)

视觉机每完成一次检测就写一次,**整块原子写**。

| D 地址 (Cam1) | D 地址 (Cam2) | 字段 | 类型 |
|:-:|:-:|:--|:-:|
| `D70-D73` | `D90-D93` | `output_x` | float64 (4 字) |
| `D74-D77` | `D94-D97` | `output_y` | float64 |
| `D78-D81` | `D98-D101` | `output_angle` 角度(度) | float64 |
| `D82` | `D102` | `result` 总判定结果 | uint16 (1=OK, 2=NG/EXCEPTION) |
| `D83-D84` | `D103-D104` | `area` 面积 | uint32 |
| `D85-D86` | `D105-D106` | `circularity` 圆度 | float32 |

> **`output_x` / `output_y` / `output_angle` 的具体含义随算法变**——某些算法用 `output_x` 装"侧码"(1=正面, 2=反面)而不是真坐标。详见第 3 节每个 ProductType 的"结果含义"小节。

---

## 2. 状态机命令对照

### `D1`/`D2` 触发命令(PLC 写入)

| 值 | 名称 | 含义 |
|:-:|:--|:--|
| `0` | `IDLE` | 空闲——视觉机不做事 |
| `1` | `READING_DATA` | (内部状态,PLC 不要写这个) |
| `2` | `PROCESSING_DATA` | (内部状态,PLC 不要写这个) |
| `3` | `TASK_COMPLETED` | (内部状态,PLC 不要写这个) |
| **`10`** | **`START_TASK`** | **拍一张并处理(单次)** |
| **`11`** | **`START_LOOP`** | **持续连续拍照处理** |

### `D10`/`D30` 触发模式

| 值 | 名称 | 含义 |
|:-:|:--|:--|
| `0` | `DISCONNECTED` | 不触发 |
| `1` | `HARDWARE_TRIGGER` | 硬件触发(IO 线 Line0,上升沿,20ms 防抖) |
| `2` | `SOFTWARE_TRIGGER` | 软件触发(由视觉机程序内部触发) |

> **连续模式 (`status=11`) 自动用软件触发**,`D10` 这时怎么写都不影响。

### `D14`/`D34` 产品类型(算法选择)

| 值 | 名称 | 算法 |
|:-:|:--|:--|
| `0` | `NONE` | (未启用,视觉机会跳过) |
| `1` | `TOOTHPASTE_FRONTBACK` | 牙膏正反面检测(Sobel 边缘计数) |
| `2` | `HEIGHT_CHECK` | 牙膏高度检测(列最大 Y 平均) |
| `3` | `BRUSH_HEAD` | 牙刷头正反面检测(凸包 + 上下密度对比) |

---

## 3. 算法专属参数(D15..D27 / D35..D47)

> **同一块 13 个字,不同 `product_type` 解释完全不同**。下面三张表对应三种算法。
> **写 0 表示"使用默认值"**——给空白机器开机也能跑。

### 3.1 `TOOTHPASTE_FRONTBACK = 1`(牙膏正反)

| Cam1 地址 | Cam2 地址 | 字段 | 类型 | 默认值 | 范围 / 说明 |
|:-:|:-:|:--|:-:|:-:|:--|
| `D15` | `D35` | `edge_intensity_threshold` 边缘强度阈值 | uint16 | 30 | 0-255,Sobel 输出大于这个值才算"边缘像素" |
| `D16-D17` | `D36-D37` | `front_count_threshold` 正面边缘数下限 | uint32 LE | 1000 | 边缘数 ≥ 此值 → 正面(1) |
| `D18-D19` | `D38-D39` | `back_count_threshold` 异常边缘数下限 | uint32 LE | 100 | 边缘数 < 此值 → 异常(无产品) |
| `D20` | `D40` | `roi_x1` ROI 左上 X | uint16 | 0 (= 全图) | |
| `D21` | `D41` | `roi_y1` ROI 左上 Y | uint16 | 0 | |
| `D22` | `D42` | `roi_x2` ROI 右下 X | uint16 | 0 | |
| `D23` | `D43` | `roi_y2` ROI 右下 Y | uint16 | 0 | |
| `D24-D27` | `D44-D47` | 保留 | — | 0 | |

**判定逻辑:**
```
统计 ROI 内 Sobel-X 边缘像素数 N
N <  back_count_threshold  → EXCEPTION (无产品)
N >= front_count_threshold → 正面 (1)
否则                        → 反面 (2)
```

**结果含义:**

| 寄存器 | 含义 |
|:--|:--|
| `output_x` | **侧码**:1=正面, 2=反面, 0=异常(无产品) |
| `output_y` | 边缘像素数(参考,可在 HMI 显示用于调阈值) |
| `output_angle` | 0 |
| `result` | 1 = OK(正反都算 OK), 2 = NG/EXCEPTION |

**约束:**`front_count_threshold` 必须大于 `back_count_threshold`,否则视觉机记录警告并使用默认值。

---

### 3.2 `HEIGHT_CHECK = 2`(牙膏高度)

| Cam1 地址 | Cam2 地址 | 字段 | 类型 | 默认值 | 范围 / 说明 |
|:-:|:-:|:--|:-:|:-:|:--|
| `D15` | `D35` | `channel` 颜色通道 | uint16 | 2 | 0=R, 1=G, **2=B** |
| `D16` | `D36` | `pixel_threshold` 像素阈值 | uint16 | 100 | 0-255,通道亮度大于此值 → 视为"有内容" |
| `D17` | `D37` | `min_height` 最低有效 Y | uint16 | 100 | 没有任何列达到此 Y → 判 EMPTY(空管) |
| `D18` | `D38` | `decision_threshold` 判定阈值 | uint16 | 300 | 列最大 Y 平均与此比较 |
| `D19` | `D39` | `roi_x1` | uint16 | 0 | |
| `D20` | `D40` | `roi_y1` | uint16 | 0 | |
| `D21` | `D41` | `roi_x2` | uint16 | 0 | |
| `D22` | `D42` | `roi_y2` | uint16 | 0 | |
| `D23-D27` | `D43-D47` | 保留 | — | 0 | |

**判定逻辑:**
```
对 ROI 中的指定通道做阈值化(>pixel_threshold 视为白)
对每一列求最大 Y(列上的最低白点)
最大 10 列的 Y 平均 = max_y_avg

如果没有任何列的最大 Y 超过 min_height → 空管 (3)
否则:
  max_y_avg <  decision_threshold → 正常 (1, OK)
  max_y_avg >= decision_threshold → 偏高/溢出 (2)
```

**结果含义:**

| 寄存器 | 含义 |
|:--|:--|
| `output_x` | **状态码**:1=正常, 2=偏高, 3=空管, 0=异常 |
| `output_y` | `max_y_avg`(参考) |
| `output_angle` | 0 |
| `result` | 1 = 决出了状态(包括 OK 和偏高), 2 = 空管或异常 |

> ⚠️ **图像 Y 坐标向下增长**:`max_y_avg` 越小,牙膏越高。如果你的相机倒装,需要在 HMI 上把 OK / 偏高 显示文字对调。

---

### 3.3 `BRUSH_HEAD = 3`(牙刷头正反)

| Cam1 地址 | Cam2 地址 | 字段 | 类型 | 默认值 | 范围 / 说明 |
|:-:|:-:|:--|:-:|:-:|:--|
| `D15` | `D35` | `shrink_pct` ROI 收缩百分比 | uint16 | 15 | 5-30,裁掉边缘干扰 |
| `D16` | `D36` | `adapt_block` 自适应阈值块大小 | uint16 | 31 | 3-99,**强制奇数** |
| `D17` | `D37` | `adapt_C` 自适应阈值常数 | int16 | 8 | -128 到 127(**带符号**) |
| `D18` | `D38` | `dot_area_min` 单个点最小面积 | uint16 | 20 | 像素 |
| `D19` | `D39` | `dot_area_max` 单个点最大面积 | uint16 | 500 | 像素 |
| `D20-D21` | `D40-D41` | `roi_area_min` ROI 最小面积 | uint32 LE | 50000 | |
| `D22-D23` | `D42-D43` | `roi_area_max` ROI 最大面积 | uint32 LE | 500000 | |
| `D24` | `D44` | `roi_ratio_min × 10` ROI 长短边比下限 | uint16 | 15 (= 1.5) | 实际比例 = 寄存器值 ÷ 10 |
| `D25` | `D45` | `roi_ratio_max × 10` ROI 长短边比上限 | uint16 | 35 (= 3.5) | |
| `D26-D27` | `D46-D47` | 保留 | — | 0 | 未来用作手动 ROI 矩形 |

**判定逻辑:**
```
1. 自适应阈值找出所有点(面积在 dot_area_min..dot_area_max 之间)
2. 至少 10 个点才能形成凸包
3. 凸包外接矩形面积 / 长短边比例符合范围
4. 旋转图像让长边水平
5. 收缩 shrink_pct% 去边缘
6. 上下两半分别再做自适应阈值,统计黑像素密度
   上半密 > 下半密 → 正面 (1)
   下半密 > 上半密 → 反面 (2)
   相等           → NG (0)
```

**结果含义:**

| 寄存器 | 含义 |
|:--|:--|
| `output_x` | **侧码**:1=正面, 2=反面, 0=NG |
| `output_y` | 0 |
| `output_angle` | 0 |
| `result` | 1 = OK(找到了正反), 2 = NG/EXCEPTION |

---

## 4. 标准操作流程(STL 风格示例)

### 4.1 单次检测(`START_TASK`)

```
PLC 侧                              视觉机侧
 │                                    │
 │ ① 配好 D10..D27 (Cam1) 参数         │
 │   product_type, 触发模式, 阈值, ROI │
 │                                    │
 │ ② D1 = 10  (START_TASK)            │
 │ ───────────────────────────────►   │
 │                                    │ 读 D1+D10..D27(原子块读)
 │                                    │ 触发拍照,运行算法
 │                                    │ ──┐
 │                                    │   │
 │                                    │ 写 D70..D86 结果(原子块写)
 │                                    │ ◄─┘
 │                                    │
 │                                    │ 写 D123 = 3 (TASK_COMPLETED)
 │ ◄───────────────────────────────   │
 │                                    │
 │ ③ 监 D123 == 3                      │
 │   读 D70..D86 取结果                │
 │   D1 = 0 (回到 IDLE)                │
 │ ───────────────────────────────►   │
```

### 4.2 连续检测(`START_LOOP`)

```
PLC 侧                              视觉机侧
 │                                    │
 │ ① 配好参数                          │
 │ ② D1 = 11 (START_LOOP)             │
 │ ───────────────────────────────►   │
 │                                    │ 进入连续模式
 │                                    │ 软触发拍照
 │                                    │
 │                                    │ 写结果块 D70..D86
 │                                    │ (每帧都写)
 │ ◄───────────────────────────────   │
 │                                    │
 │ ③ 不停读 D70..D86 取最新结果         │
 │                                    │
 │ ...                                 │ ...
 │                                    │
 │ ④ D1 = 0  (停连续)                  │
 │ ───────────────────────────────►   │
 │                                    │ 退出连续模式
 │                                    │ 写 D123 = 0 (IDLE)
 │ ◄───────────────────────────────   │
```

### 4.3 心跳监控(必做)

| 信号 | 周期 | 由谁写 | 用途 |
|:--|:-:|:--|:--|
| `D122` system_heartbeat | 1 秒 | 视觉机翻转 0↔1 | **PLC 必须监视**:连续 5 秒不变化 → 视觉机已挂 |
| `D50` plc_heartbeat | 1 秒 | PLC 翻转 0↔1 | 视觉机只读不用,纯供你诊断 |

**PLC 推荐处理逻辑:**
- 监视 `D122` 在最近 5 秒内有翻转 → 视觉机正常
- 5 秒内无翻转 → 报警(视觉机进程挂掉或网络断开)
- 同时检查 `D120 system_status`:0/1/2 正常,3 表示视觉端报错(读 `D121 error_code`)

---

## 5. 常用参数取值参考

### 像素物理距离 `pixel_distance` (D12-D13)

`float32` 编码,**小端字序**(low word 在前):

| 实际值 (mm/像素) | 应填入 D12-D13 (16进制) |
|:-:|:-:|
| 1.0 | `0x0000 0x3F80` |
| 0.5 | `0x0000 0x3F00` |
| 0.1 | `0xCCCD 0x3DCC` |

> 大多数三菱 / 西门子 PLC 都有"REAL 转 D 寄存器"指令,直接 `MOV K0.1 D12` 之类即可,不用手算。

### `roi_ratio_min/max` ×10 编码(BRUSH_HEAD)

| 想要的比例 | 应填入寄存器 |
|:-:|:-:|
| 1.5 | 15 |
| 2.0 | 20 |
| 3.0 | 30 |
| 3.5 | 35 |

---

## 6. 常见问题排查

| 现象 | 可能原因 | 解决 |
|:--|:--|:--|
| `D122` 心跳不变 | 视觉程序挂了 / 网线断了 | 看 `/home/pi/my_app.log`;或重启 systemd |
| `result=2` 持续不变 | `front_count_threshold` 设太高 / `back_count_threshold` 设太低 | 在 HMI 显示 `output_y`(边缘数),根据实际值调阈值 |
| 单次模式不响应 | `D1` 不是 10 / 之前的状态没回 IDLE | 先写 0 到 IDLE 再写 10 |
| 双相机其中一个不响应 | `config.json` 里 `enabled: false` | SSH 到视觉机改 config.json 再重启 |
| `pixel_distance` 写入后无效 | float32 字序错了 | 视觉端按 LE 解码:**low word 在小地址**(D12 是 low,D13 是 high) |
| 寄存器值看着像 negative | `D17 adapt_C` 是 **int16 带符号**,其他都是 uint16 | 写负值前先确认是不是这个寄存器 |

---

## 7. 寄存器全景图(打印贴墙用)

```
═══════════════════════ 视觉系统 PLC 寄存器映射 ═══════════════════════

  ┌─ 系统级 ─┐    ┌─ Cam1 配置(PLC 写)─┐  ┌─ Cam2 配置 ─┐
  │ D50  PLC ❤  │    │ D1   触发命令(状态)  │  │ D2  状态     │
  │ D120 状态   │    │ D10  触发模式        │  │ D30 模式     │
  │ D121 错误码 │    │ D11  曝光            │  │ D31 曝光     │
  │ D122 视觉❤ │    │ D12-D13 像素距离 f32 │  │ D32-D33      │
  │ D123 cam1   │    │ D14  产品类型        │  │ D34 产品类型 │
  │ D124 cam2   │    │ D15-D27 算法专属(13)│  │ D35-D47 (13)│
  └─────────┘    └─────────────────────┘  └─────────────┘
                
  ┌─ Cam1 结果(视觉写)──────┐  ┌─ Cam2 结果 ────────┐
  │ D70-D73 output_x f64     │  │ D90-D93   output_x  │
  │ D74-D77 output_y f64     │  │ D94-D97   output_y  │
  │ D78-D81 output_angle f64 │  │ D98-D101  angle     │
  │ D82     result u16       │  │ D102      result    │
  │ D83-D84 area u32         │  │ D103-D104 area      │
  │ D85-D86 circularity f32  │  │ D105-D106 circ      │
  └────────────────────────┘  └───────────────────┘

═════════════════════════════════════════════════════════════════════
```

---

---

# 附录:Legacy fronback 协议(老牙膏现场)

> 这一节**只给沿用原 `toothpastefronback` 程序的现场客户**看。新软件升级到此版本时,**PLC 程序、寄存器、HMI 数值全部不动**——`config.json` 写一行就切换:
>
> ```json
> { "plc_protocol": "legacy_fronback" }
> ```
>
> 不写或写 `"v2_unified"` 走前面所有章节的新协议。

## L.1 寄存器全表

### 系统级 + 配置(PLC 写,视觉读)

| D 地址 | 类型 | 名称 | 用途 |
|:-:|:-:|:--|:--|
| `D1` | uint16 | capture_trigger | 写 10 触发拍照,视觉处理完写回 0/1 |
| `D2` | uint16 | workcamera_count | **模式开关**:1=双相机正反, 0=单相机高度, 2=单相机牙刷头(v0.3.14+) |
| `D10` | uint16 | cam1_exposure | 正反模式 cam1 曝光(微秒);牙刷头模式同样用此寄存器 |
| `D11` | uint16 | cam2_exposure | 正反模式下 cam2 曝光(微秒) |
| `D12` | uint16 | brush_dot_area_min | **牙刷头模式**最小斑点面积;0 = 用 config.json 默认 |
| `D13` | uint16 | brush_dot_area_max | 牙刷头模式最大斑点面积;0 = 默认 |
| `D14` | uint16 | brush_ratio_min × 10 | 牙刷头 ROI 长短边比下限 ×10(15 = 1.5);0 = 默认 |
| `D15` | uint16 | brush_ratio_max × 10 | 牙刷头 ROI 长短边比上限 ×10;0 = 默认 |
| `D30` | uint16 | height_cam2_exposure | 高度模式下 cam2 曝光 |
| `D31` | uint16 | brightness_threshold | 高度模式亮度阈值(0-255) |
| `D32` | uint16 | min_height | 高度模式最低有效 Y |
| `D33` | uint16 | left_limit | 高度模式列检测 ROI 左边界(0=不限);v0.3.15+ 已生效 |
| `D34` | uint16 | right_limit | 高度模式列检测 ROI 右边界(0=不限);v0.3.15+ 已生效 |
| `D35` | uint16 | height_comparison | 高度模式判定阈值 |
| `D36` | uint16 | width_comparison | 读但不用(协议保持) |

### 结果(视觉写,PLC 读)

| D 地址 | 类型 | 名称 | 用途 |
|:-:|:-:|:--|:--|
| `D0` | uint16 | recognition_result | **结果**:1=正面/OK, 2=反面/NG, 3=空管(仅高度模式) |
| `D1` | uint16 | capture_trigger | 视觉机回写:0=收到处理中, 1=完成 |
| `D3` | uint16 | cam1_status | 1=cam1 在线, 0=离线 |
| `D4` | uint16 | cam2_status | 同上 |
| `D20-D21` | uint32(LE 字序) | edge1_count | 正反模式 cam1 边缘像素数 |
| `D22-D23` | uint32(LE 字序) | edge2_count | 正反模式 cam2 边缘像素数 |
| `D40` | uint16 | height_result | 高度模式 top-10 列最大Y平均 |
| `D41` | uint16 | width_result | 占位,目前不写 |
| `D42` | uint16 | brush_dot_count | **牙刷头模式**检测到的斑点数(诊断用,可不读)|
| `D43` | uint16 | brush_area_x100 | 牙刷头检测 ROI 面积 ÷ 100(诊断用) |

## L.2 标准操作时序

### 双相机正反 (D2=1)

```
PLC                                   视觉机
 │                                      │
 │ ① 配 D10/D11 曝光                    │
 │ ② D2 = 1 (正反模式)                   │
 │ ③ D1 = 10 (触发)                      │
 │ ───────────────────────────────►    │
 │                                      │ 块读 D1+D2 → 看到触发
 │                                      │ 写 D1 = 0 (确认收到)
 │ ◄───────────────────────────────    │
 │                                      │ 块读 D10+D11 → 设两相机曝光
 │                                      │ 并行采图 cam1+cam2
 │                                      │ 算 Sobel 边缘数
 │                                      │ result = (e1 > e2) ? 1 : 2
 │                                      │ 并行写 D0, D3, D4, D20-23
 │                                      │ 写 D1 = 1 (完成)
 │ ◄───────────────────────────────    │
 │                                      │
 │ ④ 监 D1 == 1 → 读 D0 取结果            │
 │   读 D20-23 取边缘数(可选,调阈值用)  │
```

### 单相机高度 (D2=0)

```
PLC                                   视觉机
 │                                      │
 │ ① 配 D30 曝光,D31 亮度阈值,           │
 │   D32 最低有效Y, D35 判定阈值          │
 │ ② D2 = 0 (高度模式)                   │
 │ ③ D1 = 10 (触发)                      │
 │ ───────────────────────────────►    │
 │                                      │ 块读 D1+D2
 │                                      │ 写 D1 = 0
 │ ◄───────────────────────────────    │
 │                                      │ 块读 D30..D36 (一次 7 寄存器)
 │                                      │ 设 cam2 曝光,采 cam2
 │                                      │ R 通道阈值化,列最大Y top10 平均
 │                                      │ result = 1/2/3 (OK/NG/空)
 │                                      │ 并行写 D0, D4, D40
 │                                      │ 写 D1 = 1
 │ ◄───────────────────────────────    │
 │                                      │
 │ ④ 监 D1 == 1 → 读 D0 + D40             │
```

**屏幕显示叠加(v0.3.15+)**:操作员屏上的 cam2 帧会叠加三种 overlay,所有 overlay 在对应 PLC 字段为 0 时不绘制(byte-compat 默认):
- **黄色矩形竖带**:D33 ~ D34 之间 = 列检测 ROI(让操作员看到算法在哪里测高度)
- **红色水平线**:y = D35 = 判定阈值(高于线 → NG;低于 → OK)
- **蓝色短竖线**:top-10 列的 max_y 位置(算法实际取了哪些列做平均)

### 单相机牙刷头 (D2=2,v0.3.14+)

> **可选 mode**:复用 v2 的 BrushHeadProcessor 算法。客户 PLC ladder 必须显式 dispatch D2=2,但**算法参数全部可选**——D12-D15 写 0 时使用 `config.json:legacy_brush_head_defaults` 段的值。

```
PLC                                   视觉机
 │                                      │
 │ ① 配 D10 cam1 曝光(必)               │
 │   D12-D15 算法参数(可选;0=默认)     │
 │ ② D2 = 2 (牙刷头模式)                 │
 │ ③ D1 = 10 (触发,或 D1=11 进 LOOP)     │
 │ ───────────────────────────────►    │
 │                                      │ 块读 D1-D15 (LOOP 一次)
 │                                      │ 写 D1 = 0
 │ ◄───────────────────────────────    │
 │                                      │ 设 cam1 曝光,采 cam1
 │                                      │ 自动检测 dot 凸包 + 长短边比验证
 │                                      │ result = 1 (OK 含 Front/Back) / 2 (NG)
 │                                      │ 并行写 D0, D3, D42, D43
 │                                      │ 写 D1 = 1 (FIRE 模式) / 不写 D1 (LOOP)
 │ ◄───────────────────────────────    │
 │                                      │
 │ ④ 监 D1 == 1 → 读 D0                  │
 │   读 D42/D43 取诊断信息(可选)        │
```

**双轨参数策略**:
- 客户 PLC 完全不改 D12-D15 → 全部走 `config.json` 默认值。**最小 PLC 改动 = 加一行 D2=2 dispatch**。
- 客户 PLC 写非 0 到某个 D12-D15 → 该参数走 PLC,其他仍走默认值。**渐进式精细控制**。
- 全部 D12-D15 写非 0 → 完全 PLC 控制,跟 frontback / height 一致。

## L.3 与原 fronback 程序的差异(全部不影响 PLC 行为)

| 项 | 原 fronback | 新 legacy adapter | 客户感知 |
|:--|:-:|:-:|:--|
| 双相机采图 | 串行(先 cam1 再 cam2) | 并行(asyncio.gather) | **采图节拍快 ~50ms,结果一致** |
| 双 PLC 写(D3+D4 / D20+D40) | 串行 | 并行 | 写完早 ~10ms |
| D12/D13 unrecognized_threshold | 每 50ms 读两次,**结果丢弃** | **不读** | 每秒少 40 次 Modbus 读,客户无感 |
| 块读 D30..D36 | 7 次单字读 | 1 次 7 字块读 | 高度模式快 ~20ms |
| 块写 D20..D23 | 4 次单字写 | 1 次 4 字块写 | 边缘数下发快 ~5ms |
| 边缘判定逻辑 | `e1 > e2` ? 1 : 2 | **完全一致** | 同一张图,**结果字节级相同** |
| 高度判定逻辑 | R 通道,top10 平均 | **完全一致** | 同上 |

## L.4 关于 `D12/D13 unrecognized_threshold`

通过对原 `toothpastefronback` 仓库的全文检索:

```
$ grep -rn unrecognized fronback/ --include="*.py"
plc_manager.py:19:  unrecognized_threshold = 0           # 全局变量声明
plc_manager.py:20:  unrecognized_threshold_low = 0
plc_manager.py:21:  unrecognized_threshold_high = 0
plc_manager.py:281: ADDRESS_THRESHOLD_UNRECOGNIZED_LOW = 12
plc_manager.py:282: ADDRESS_THRESHOLD_UNRECOGNIZED_HIGH = 13
plc_manager.py:328: # Reading the unrecognized thresholds
plc_manager.py:329: global unrecognized_threshold,unrecognized_threshold_low, unrecognized_threshold_high
plc_manager.py:330: unrecognized_threshold_low = plc_communicator.read_data(...)[0]
plc_manager.py:331: unrecognized_threshold_high = plc_communicator.read_data(...)[0]
plc_manager.py:332: unrecognized_threshold = (unrecognized_threshold_high << 32) | low
```

**所有 8 处出现都在 `plc_manager.py`,且没有任何地方读取或使用这两个变量的值**(算法、判定、写回都不涉及它们)。原代码读了 D12/D13 后赋给全局变量然后再没用过。

新版 legacy adapter 直接**不读 D12/D13**,每次循环少两次 Modbus 来回。如果客户那边发现哪个 ladder 真的需要"视觉机定期访问 D12/D13"(理论上不可能),改回去也是一行代码的事。

## L.5 ROI 配置文件

每台相机一份 JSON,跟原版一样:

```
roi_coordinates_192_168_2_10.json     # cam1 (IP 中的点用下划线代替)
roi_coordinates_192_168_3_10.json     # cam2
```

文件内容:
```json
{"x1": 300, "y1": 100, "x2": 950, "y2": 850}
```

**部署时直接把现场用的两份文件 scp 到二进制同一目录**——格式和原 fronback 完全一致,不用动。

模板见仓库 [`legacy/sample_roi/roi_coordinates_template.json`](../legacy/sample_roi/roi_coordinates_template.json)。

---

> 文档版本:v0.2.0 配套(legacy 协议适配层引入)
> 对应代码 commit:见 [`README.md`](../README.md)
> 有疑问请到仓库提 issue
