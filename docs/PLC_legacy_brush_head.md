# PLC 寄存器:Legacy Brush_head (D2=2)

单相机(cam1)牙刷头正反检测。v0.3.16+。

## 系统寄存器(三模式共用)

| 地址 | 方向 | 类型 | 名称 | 值 |
|:-:|:-:|:-:|:--|:--|
| `D0` | 视觉→PLC | uint16 | result | 1=OK / 2=NG |
| `D1` | PLC→视觉 | uint16 | trigger | 10=FIRE / 11=LOOP / 0=stop |
| `D1` | 视觉→PLC | uint16 | trigger ack | 0=ack / 1=done |
| `D2` | PLC→视觉 | uint16 | mode | **写 2 选 brush_head** |
| `D3` | 视觉→PLC | uint16 | cam1_status | 1=online / 0=offline |
| `D4` | 视觉→PLC | uint16 | cam2_status | 同上 |
| `D9` | 视觉→PLC | uint16 | system_heartbeat | 每秒翻 0/1,PLC watchdog 监视 |

## PLC 写

任意字段写 0 = 用 `config.json:legacy_brush_head_defaults` 默认值。

| 地址 | 类型 | 名称 | 默认 | 编码 |
|:-:|:-:|:--|:-:|:--|
| `D50` | uint16 | cam1_exposure | 5000 | μs |
| `D51` | uint16 | shrink_pct | 15 | % |
| `D52` | uint16 | adapt_block | 31 | 奇数像素 |
| `D53` | — | reserved | — | — |
| `D54` | uint16 | dot_area_min | 20 | 像素² |
| `D55` | uint16 | dot_area_max | 500 | 像素² |
| `D56` | uint16 | roi_area_min ÷100 | 500 | × 100 = 像素² |
| `D57` | uint16 | roi_area_max ÷100 | 5000 | × 100 = 像素² |
| `D58` | uint16 | ratio_min × 10 | 15 | ÷ 10 = ratio |
| `D59` | uint16 | ratio_max × 10 | 35 | ÷ 10 = ratio |
| `D60` | uint16 | manual_roi.x1 | 0 | 像素 |
| `D61` | uint16 | manual_roi.y1 | 0 | 像素 |
| `D62` | uint16 | manual_roi.x2 | 0 | 像素 |
| `D63` | uint16 | manual_roi.y2 | 0 | 像素;(0,0,0,0)=auto |

## PLC 读

| 地址 | 类型 | 名称 |
|:-:|:-:|:--|
| `D0` | uint16 | result(1=OK / 2=NG)|
| `D70` | uint16 | brush_side_code(1=Front / 2=Back / 0=UNKNOWN)|

D0 + D70 配套使用:

| D0 | D70 | 含义 |
|:-:|:-:|:--|
| 1 | 1 | OK,正面 |
| 1 | 2 | OK,反面 |
| 2 | 0 | NG(检测失败)|

> v0.3.27 起移除了 D42/D43 占位寄存器(原 brush_dot_count / brush_area,固定写 0 没意义)。需要 dot count / area 真实诊断时,会重新加到 D72/D73。

## 触发

```
单次:  PLC 写 D2=2, D1=10  →  视觉跑完写 D0/D70 + D1=1
LOOP:  PLC 写 D2=2, D1=11  →  视觉持续跑,每 cycle 写 D0/D70
                            →  PLC 写 D1=0 停止
```
