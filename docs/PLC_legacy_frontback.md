# PLC 寄存器:Legacy Frontback (D2=1)

双相机正反检测。

## 系统寄存器(三模式共用)

| 地址 | 方向 | 类型 | 名称 | 值 |
|:-:|:-:|:-:|:--|:--|
| `D0` | 视觉→PLC | uint16 | result | 1=Front / 2=Back |
| `D1` | PLC→视觉 | uint16 | trigger | 10=FIRE / 11=LOOP / 0=stop |
| `D1` | 视觉→PLC | uint16 | trigger ack | 0=ack / 1=done |
| `D2` | PLC→视觉 | uint16 | mode | **写 1 选 frontback** |
| `D3` | 视觉→PLC | uint16 | cam1_status | 1=online / 0=offline |
| `D4` | 视觉→PLC | uint16 | cam2_status | 同上 |
| `D9` | 视觉→PLC | uint16 | system_heartbeat | 每秒翻 0/1,PLC watchdog 监视 |

## PLC 写

| 地址 | 类型 | 名称 | 单位 |
|:-:|:-:|:--|:--|
| `D10` | uint16 | cam1_exposure | μs |
| `D11` | uint16 | cam2_exposure | μs |

## PLC 读

| 地址 | 类型 | 名称 |
|:-:|:-:|:--|
| `D0` | uint16 | result(1=Front / 2=Back)|
| `D20-D21` | uint32 LE | edge1_count |
| `D22-D23` | uint32 LE | edge2_count |

## 触发

```
单次:  PLC 写 D2=1, D1=10  →  视觉跑完写 D0/D20-23 + D1=1
LOOP:  PLC 写 D2=1, D1=11  →  视觉持续跑,每 cycle 写 D0/D20-23
                            →  PLC 写 D1=0 停止
```
