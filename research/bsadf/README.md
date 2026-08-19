# BSADF 有效样本量工具

为开题报告表 4「有效样本量的分层推算」生成实测数字：把币-日行数依次收敛到
**通过 GSADF 检验的币种 → 币内独立 episode → 全市场独立周期**，最后一项才是双向聚类
下的有效 cluster 数，也是主模型自变量个数的上限。

## 方法

Phillips, Shi and Yu (2015) 的递归右尾单位根检验，ADF 设定为

```
dy_t = alpha + beta * y_{t-1} + sum_{j=1..k} phi_j * dy_{t-j} + e_t
```

统计量为 `beta` 的右尾 t 比。最小窗口用 PSY 的 `r0 = 0.01 + 1.8/sqrt(T)`。

标签采用**两阶段**程序，这一点与很多应用文献的做法不同，也是本实现的关键：

1. **GSADF 门槛**：先用 sup 分布临界值检验该币种是否至少存在一次爆炸过程。
   未通过者根本不进入日期标定。
2. **路径级校准band**：通过者再用 BSADF 序列做日期标定，但比较对象不是逐点分位数，
   而是加了常数偏移后的band，偏移量由模拟标定，使**整条路径**在零假设下的假警报率
   等于 alpha（Phillips and Shi, 2020 的实时监测逻辑）。

原因见 `tests/test_psy.py::test_pointwise_band_is_not_size_controlled`：
逐点 95% 分位数序列不是 size-controlled band——因为路径被检视 T 次，
一条无漂移随机游走以远高于 5% 的概率在某处越线（实测 > 50%）。
在 30-50 个币种上直接逐点标定，会按构造产生大量横截面假标签。
工具同时输出「无门槛 + 逐点band」的 episode 数，二者之差即预期假标签量。

## 验证

`tests/test_psy.py` 覆盖四件事：

- 模拟临界值与 PSY (2015) 公布的有限样本 GSADF 表一致（T=200/400，容差 0.22）
- 校准band的路径级经验 size 接近名义水平
- 逐点band的过度标记行为（回归保护）
- 植入的爆炸段能被检出并定位到真实起点 ±45 天内

```
python -m pytest research/bsadf/tests -q
```

## 运行

```bash
pip install -r research/requirements.txt

# 正式运行：2021-2025，默认 50 币的非稳定币池
cd research && python -m bsadf.cli \
    --source binance --start 2021-01-01 --end 2025-12-31 \
    --reps 2000 --out out/

# 已有授权数据时（<SYMBOL>.csv，列为 date,close）
cd research && python -m bsadf.cli --source csv --csv-dir /path/to/prices --out out/

# 仅验证管道（合成数据，不是研究输入）
cd research && python -m bsadf.cli --source synthetic --reps 500 --out out/
```

输出 `out/result.json`（逐币、逐 episode、逐周期的完整明细）与 `out/table4.md`
（可直接粘进开题报告的表 4）。

主要参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--lags` | 0 | ADF 滞后阶；0 对应 PSY 临界值表的设定 |
| `--level` | 0.95 | 临界水平；稳健性应另跑 0.90 与 0.99 |
| `--reps` | 2000 | 模拟重复次数；不应低于 2000（pilot 的 99 次远远不够） |
| `--min-duration` | `round(log T)` | 最短持续期 |
| `--merge-gap` | 30 | 间隔小于此值的标记段合并为同一 episode |
| `--cycle-window` | 30 | 进入日相隔不超过此值的 episode 归为同一全市场周期 |

`--merge-gap` 与 `--cycle-window` 直接决定表 4 的后两行，属于附录 A 必须冻结的规则，
请在打开时间外评价期之前固定，并对 15/30/45 日做敏感性。

## 数据

`--source binance` 走 `api.binance.com` 现货日线；`--source coingecko` 为备份。
若所在环境禁止访问交易所 API，先离线取数再用 `--source csv`。

币池默认排除稳定币、杠杆/反向代币与 wrapped 重复（见 `data.EXCLUDED`），
但**尚未做时点化处理**：默认列表是当前存续币种，直接使用会带来生存者偏差。
正式运行前请按 4.1 节用历史上市记录替换 `DEFAULT_UNIVERSE`，并保留已退市资产。
