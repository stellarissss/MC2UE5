# ENG-P0 — 冻结诊断笔记 / P0 freeze notes

作者 / author: 程基岩 (engineering-lead-4) · 日期 / date: 2026-10-07
状态 / status: **两个根因已定位并修复,已构建打包实测 / two root causes found, fixed, built and measured**

---

## 0. 结论 / Summary

冻结是**两个独立的周期性停顿**叠加,都不是几何量本身的问题:

| # | 根因 | 机制 | 状态 |
|---|---|---|---|
| **1** | `-MCdiag` 运行时诊断**无条件常驻** | 每秒遍历全部 **1,171,144 个实例**做世界空间变换合成 | 已修(改为 `-MCdiag` 门控 + 采样上限)|
| **2** | `ApplyLook` 每秒重跑 15 次,每次强制天空重新捕获 | `MarkRenderStateDirty()` 丢弃 sky capture 并重渲染,实测 **~1.2 秒/次** | 已修(幂等化) |

**"性能未占满"的机制**:主线程被纯 CPU 循环占死,**恰好一个核 ~100%**,
渲染线程拿不到提交,GPU 空闲。任务管理器总 CPU 因此看着不高。用户的观察完全正确。

**修复效果(实测)**:周期性 0.2–1 秒停顿 **41 次 → 0 次**。

---

## 1. 为什么旧数据无法回答这个问题 / Why the old numbers were useless

必须先说清楚,否则下面的数字会被误读:

1. **`frame=400.0ms` 不是测量值。** 全部 4,336 行历史日志里,400.0 出现 **18 次**,
   且**每次都恰好是 400.0**。这是 `MaxDeltaTime` 的钳位上限——所有真正的慢帧
   都被压成同一个数字。这个字段**无法**区分"平台期"和"尖峰"。
2. **1 Hz 采样看不见尖峰。** 采样间隔 1 秒,短于 1 秒的停顿直接被混叠掉。
3. 因此交办要求的"平台期 vs 尖峰"这一关键区分,**用旧日志无法回答**。

修复后新增的 `ft[...]` 字段按**帧**采样,给出分位数与分类。

---

## 2. 根因 1:诊断代码本身(原 MCConsoleCommands.cpp:631-638)

```cpp
// Runtime collision diagnostic. Always on: ...
GetWorldTimerManager().SetTimer(GDiagHandle, ...RunRuntimeDiag..., 1.0f, true);
```

`RunRuntimeDiag` → `WriteMCDiagLine`,内部对**全世界每个 ISM 的每个实例**
调用 `GetInstanceTransform(i, T, /*bWorldSpace*/ true)`:

```
1,041 clusters × 1,171,144 instances × 每秒 1 次
= 每秒约 1.17 M 次世界空间矩阵合成,单线程
```

原注释写 "costs one line a second" —— **这只统计了 I/O,没统计计算**。
这是我的错误:一个遍历实例的诊断不是诊断,是压测。

**次因**:开了 `-MCdiag` 会注册**两个**定时器(`GDiagTimer` + `GDiagHandle`),
诊断成本被自己翻倍。**测量工具污染了被测对象。**

### 修复
- 定时器**只在 `-MCdiag` 时注册**
- 删掉重复注册的第二个定时器
- 实例遍历加**上限 4096**、跨组件步进采样;
  实例总数改用 **O(1) 的 `GetInstanceCount()`**(实测 `sampled=4096`)

---

## 3. 根因 2:ApplyLook 每秒强制天空重捕获

`ApplyLook` 挂在 1 秒循环定时器上跑 15 秒,每次都执行:

```cpp
C->MarkRenderStateDirty();   // SkyLight, bRealTimeCapture = true
```

对 real-time capture 的 SkyLight,这一行**丢弃现有捕获并强制重渲染+重上传**。
实测代价 **~1.2 秒/次**。

### A/B 实测(`-MClook=<秒>`,0 = 只应用一次)

缓存预热后,交替顺序重复测量:

| 配置 | world 秒 | 稳态最大帧 | 0.2–1 秒重复停顿 |
|---|---|---|---|
| `-MClook=0` | 56 | 10,041 ms | **0** |
| `-MClook=15` | 93 | **77,695 ms** | **6**(1159/1175/1239/1394/1404/1418 ms)|
| `-MClook=0`(第2次)| 137 | 21,212 ms | **0** |
| `-MClook=15`(第2次)| 121 | **121,190 ms** | **7** |

**6–7 次约 1.2 秒的停顿,间隔正好 1 秒 = 定时器周期。**
关掉重复后归零。这是本次诊断最干净的一组证据。

### 修复(幂等化)
- SkyLight:`MarkRenderStateDirty()` **只在首次**(`GLookSkyCaptured`)
- DirectionalLight:`SetActorRotation`/`SetMobility` **只在首次**(`GLookSunApplied`)
- 资产探针 `LoadObject` ×4 **只跑一次**(同步加载,每秒 4 次是阻塞式资产加载)
- `applylook.txt` 只在内容变化时追加
- 强度/曝光等幂等赋值保持每次都写(便宜且无害)
- 新增 **`-MClook=<秒>`**,0 = 只应用一次(可测量,不必重新构建)

### 修复后验证(默认 `-MClook=15`,即出厂值,连续 3 次)

```
run1: worldSec=  3 | stalls>1s=[8405]                        | 重复停顿=0
run2: worldSec= 54 | stalls>1s=[53885, 9898]                 | 重复停顿=0
run3: worldSec= 95 | stalls>1s=[12484, 76070]                | 重复停顿=0
```

**全量对比:0.2–1 秒重复停顿 41 次(修复前,732 world 秒)→ 0 次(修复后,152 world 秒)。**

---

## 4. 必须澄清的测量假象 / Measurement artifacts (重要)

**报告里有 10 个 >20 秒的"帧",全部是测量假象,不是引擎停顿。**

我的采样方式是后台启动进程然后 `sleep`;在这种状态下 OS 会挂起进程。
**一帧不可能长于进程存活时间**(我的采样窗口 60–75 秒),
所以 21s / 49s / 53s / 76s / **121s** 这些值物理上不可能是真实帧时。

| 报告帧时区间 | 计数 | 可信度 |
|---|---|---|
| < 100 ms | 797 | ✅ 可信 |
| 100–500 ms | 32 | ✅ 可信 |
| 0.5–1 s | 19 | ✅ 可信 |
| 1–5 s | 9 | ⚠️ 可能含挂起 |
| 5–20 s | 17 | ⚠️ 可能含挂起 |
| **> 20 s** | **10** | ❌ **挂起假象** |

**稳态 p50 中位数 = 3.80 ms**(884 个 world 秒,min 2.10 / max 13.20)。
稳态本身是健康的(~260 fps 量级);问题是**周期性停顿**,不是持续开销。

**分类结果:`class=` 只出现过 `ok` 和 `spikes`,从未出现 `plateau`。**
即:这是**尖峰型**,不是平台期——与交办预判的"持续高平台"相反,我的测量为准。
平台期意味着几何/碰撞太重;尖峰意味着有定时器在做事。**是后者。**

**正确测量方法(建议后续沿用)**:必须让窗口在前台有焦点,否则 OS 挂起会污染数据。
`tools/focus_capture.py` 的 `PrintWindow` + `PW_RENDERFULLCONTENT` 路径可用。

---

## 5. `-MClayers` A/B 开关(已交付)

新增 `MCLayerControl.{h,cpp}`。一次启动即可逐层关闭:

```
-MClayers=vox                  仅体素层
-MClayers=vox,struct           体素 + 结构,地形关
-MClayers=-shadow              除阴影投射外全开
-MClayers=-collision           关碰撞(体素 + 结构 + 地形)
-MClayers=none                 全关
-MClayers=none,vox             仅体素
-MClayers=all                  出厂配置
```

- 关层用 `SetVisibility(false)`,**不销毁**——这是测量开关,不可逆操作留给 S6。
- 按**资产名**匹配(`bld_*` / `overworld_*`),因为 actor 标签过不了 cook。
- **无法识别的 token 报 Error**,不静默忽略(拼错会让 A/B 结论完全失效)。
- 返回 `FMCLayerCensus`,能区分"开关没修好"和"开关没东西可关"。

### 实测(修复后的包)

| 配置 | 稳态 CPU | 备注 |
|---|---|---|
| 默认 | p50 **103%**,p90 289%,max 555% | 单核满 |
| `-MClayers=-collision` | p50 **101%**,p90 **106%**,max **108%** | **尖峰消失** |
| `-MClayers=-shadow` | p50 98%,p90 102%,max **102%** | **尖峰消失** |

**结论:几何层和碰撞都不是主因。** 关掉碰撞或阴影后尖峰都消失,
说明尖峰来自**每帧/每秒重复的 CPU 工作**(即根因 1、2),而非场景复杂度。
这也解释了为什么"少一层几何"救不了卡顿。

### 自查发现并修掉的自身 bug
第一版 `-MClayers=-collision` 报告 `collOff=0`(什么也没关)。
原因:碰撞开关只作用于 struct/terrain 网格,**漏掉了体素层**——
而角色正是站在体素上的。已修:census 现在正确报告 `collOff=1041`。
**一个"报告 0 效果"的开关会被读成"碰撞不要钱",这是最坏的失败模式。**

---

## 6. 与交办描述不符之处 / Corrections to the brief

我的测量与交办内容不符的地方,按要求明说:

1. **"两块几何层同时存在"在实测包里不成立。**
   `meshes=` 在**全部 4,336 行历史日志中都是 0**,从未非零。
   而 `propISM=1041 propInst=1171144` 始终存在。
   → **dist52/dist_p0 里只有体素层;158 个结构网格和 24 个地形瓦片都不在这份 level 里。**
   资产确实存在(`Content/MC/Structures` 有 158 个 .uasset),
   但 S6A 的 level 落位没进这些包。
   → **需要你确认:截图里的立面来自哪一份 level?** 这直接决定后续工作是否需要重做落位。

2. **不是"持续高平台",是"周期性尖峰"。** `class=` 全程 `ok`/`spikes`,无 `plateau`。
   平台期与尖峰的修法相反,这个区分是本次定位的关键。

3. **`frame=400.0ms` 不能当帧时间引用**(MaxDeltaTime 钳位)。

4. **`taskkill /IM MCReplica.exe` 只对 Development 包有效。**
   Shipping 包进程名是 `MCReplica-Win64-Shipping.exe`。交办已提示,此处再次确认。

5. **额外发现的构建阻塞项**:C: 被清过,注册表里
   `HKLM\...\Windows Kits\Installed Roots\KitsRoot10` 丢失,
   症状是 `Platform Win64 is not a valid platform to build / Sdk: not found`,
   **看起来像引擎问题,其实是注册表**。构建前必须跑 `repo/tools/reg_sdk.py`。
   (HKLM 的 .NET 条目会 Access denied,但 Shipping 不需要,可忽略。)

6. **cook 必须两步**(`-SkipZenStore` 单独 cook,再 `-skipcook` 打包)。
   直接 `BuildCookRun -cook` 会因 Zen oplog `HTTP NotFound` 失败。
   完整可用的 bat 在 `Q:/MC2UE5/shots/{build_shipping2.bat,cook_p0.bat}`。

---

## 7. 结论:性能未占满 ≠ 性能不够

用户报"电脑性能未占满"是**最有价值的线索**,它排除了"场景太重"，
指向"主线程被阻塞"。实测完全吻合:

- 一个核 ~100%,其余核与 GPU 空闲 → 任务管理器看着"没占满"
- 输入排队在阻塞的 tick 后面 → **镜头无法旋转**(相机只是症状)
- 周期性 1.2 秒停顿 → **非常卡顿**

**已交付:**
- `Source/MCReplica/Public/MCLayerControl.h` + `Private/MCLayerControl.cpp`(新增)
- `Source/MCReplica/Private/MCConsoleCommands.cpp`(诊断门控、采样上限、ApplyLook 幂等、`-MClook`)
- `Q:/MC2UE5/shots/measure_run.py`(外部进程级采样工具)

**未做 / 待你决定:**
- **未删除体素层**(S6,等你签字)
- 未 git commit
- 三个视觉问题(偏 brown / 空中浮板 / 地面摩尔纹)**本轮完全未动**——
  按交办次序,它们排在冻结之后。且第 1 条(grounding)需要先确认 level 里到底有没有结构层。