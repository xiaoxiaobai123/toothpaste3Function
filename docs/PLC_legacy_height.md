# PLC 寄存器:Legacy Height (D2=0)

单相机(cam2)高度检测。

## 系统寄存器(三模式共用)

| 地址 | 方向 | 类型 | 名称 | 值 |
|:-:|:-:|:-:|:--|:--|
| `D0` | 视觉→PLC | uint16 | result | 1=OK / 2=NG / 3=EMPTY |
| `D1` | PLC→视觉 | uint16 | trigger | 10=FIRE / 11=LOOP / 0=stop |
| `D1` | 视觉→PLC | uint16 | trigger ack | 0=ack / 1=done |
| `D2` | PLC→视觉 | uint16 | mode | **写 0 选 height** |
| `D3` | 视觉→PLC | uint16 | cam1_status | 1=online / 0=offline |
| `D4` | 视觉→PLC | uint16 | cam2_status | 同上 |

## PLC 写

| 地址 | 类型 | 名称 | 单位 / 范围 |
|:-:|:-:|:--|:--|
| `D30` | uint16 | cam2_exposure | μs |
| `D31` | uint16 | brightness_threshold | 0-255 |
| `D32` | uint16 | min_height | 像素 Y |
| `D33` | uint16 | left_limit | 像素 X,0=不限 |
| `D34` | uint16 | right_limit | 像素 X,0=不限 |
| `D35` | uint16 | height_comparison | 像素 Y |
| `D36` | uint16 | width_comparison(读但不用)| — |

## PLC 读

| 地址 | 类型 | 名称 |
|:-:|:-:|:--|
| `D0` | uint16 | result(1=OK / 2=NG overfill / 3=EMPTY)|
| `D40` | uint16 | height_result(top-10 max_y 平均)|
| `D41` | uint16 | width_result(占位,固定 0)|

## 触发

```
单次:  PLC 写 D2=0, D1=10  →  视觉跑完写 D0/D40 + D1=1
LOOP:  PLC 写 D2=0, D1=11  →  视觉持续跑,每 cycle 写 D0/D40
                            →  PLC 写 D1=0 停止
```
