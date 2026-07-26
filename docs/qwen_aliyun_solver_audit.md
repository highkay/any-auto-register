# Qwen Aliyun Solver 专项评估

## 目标

回答两个问题：

1. `qwen` 注册卡在阿里云 WAF 滑块时，问题主要在浏览器拖动，还是在 `ohmycaptcha` 返回的 gap 点位。
2. 既然项目里有本地图像纠偏，为什么还会失败，这层纠偏是否应该继续覆盖 solver 原始结果。

## 评估范围

- 只看 `qwen` 当前 WAF 滑块链路。
- 不混入 `zai` 的 `captchaVerifyParam` 回调模型。
- 以固定题图、重复求解、页面截图和拖动中间态为主证据。

相关代码：

- `qwen` 终点计算：[platforms/qwen/core.py](../platforms/qwen/core.py)
- `qwen` 多次采样与主簇选择：[platforms/qwen/core.py](../platforms/qwen/core.py)
- `AliyunSlideActionTask` 调用入口：[core/base_captcha.py](../core/base_captcha.py)

## 方法

### 1. 固定题图，重复问 solver

对同一张 `qwen` challenge 图反复调用 `solve_aliyun_slide_action(...)`，看 `gap` 是否稳定。

### 2. 固定页面截图，比较视觉位置

保留三种截图：

- 拖动前
- 拖到目标但未松手
- 松手失败后

用来判断：

- 原始 solver 点位是否和真实缺口大致一致
- 本地终点计算是否把原始点位改坏
- 失败后页面是否进入服务端 verify

### 3. 拆开两层 owner

- `raw solver gap`
- `qwen` 本地 `_resolve_slide_end_x(...)` / 浏览器拖动结果

## 核心结论

### 结论 1：不是单一 owner，而是两类失败同时存在

`qwen` 当前至少有两类失败：

1. `ohmycaptcha` 原始点位大致合理，但 `qwen` 本地纠偏把它改坏。
2. `ohmycaptcha` 原始点位自己就识别错。

所以答案不是简单的“操作不对”或“solver 不准”二选一。

### 结论 2：浏览器拖动不是当前第一 owner

已经做过这些排查：

- `closed_loop`
- `smooth`
- `overshoot`
- `patchright` 默认通道
- `patchright + chrome channel`

失败形态没有本质变化，仍然是页面本地直接 `验证失败，请重试`，且没有 `aliyunRequests` 发出。说明主要矛盾不在“拖动是不是像人”，而在“目标点是否正确”。

## 证据组 A：raw solver 基本对，但本地纠偏把它改坏

附件目录：

- [qwen_root_cause_probe](./artifacts/qwen_solver_audit/qwen_root_cause_probe/)
- [qwen_visual_probe](./artifacts/qwen_solver_audit/qwen_visual_probe/)

关键截图：

- [海边 challenge 原图](./artifacts/qwen_solver_audit/qwen_root_cause_probe/challenge.png)
- [过冲前截图](./artifacts/qwen_solver_audit/qwen_visual_probe/challenge_before.png)
- [过冲 hold 截图](./artifacts/qwen_solver_audit/qwen_visual_probe/challenge_hold.png)

关键事实：

- 对 `qwen_root_cause_probe/challenge.png` 这张固定题图，离线重复求解 9 次，`localGapX` 基本稳定在 `225-227`。
- 这和画面里右侧缺口的位置是一致的，说明这张图上 `ohmycaptcha` 原始点位是可用的。
- 但旧链路里另一张题图的 raw solver 主簇只有 `210-212`，最终真正拿去拖的 `end_x` 却到了 `273.5`。
- 对应 `qwen_visual_probe/challenge_hold.png` 可以直接看到：拼图块被拖到了最右边，明显越过缺口。

对应元数据：

- [qwen_root_cause_probe/meta.json](./artifacts/qwen_solver_audit/qwen_root_cause_probe/meta.json)
- [qwen_visual_probe/meta.json](./artifacts/qwen_solver_audit/qwen_visual_probe/meta.json)

owner 判断：

- 这类失败不是 `ohmycaptcha` 原始返回直接错。
- 真正把结果拖坏的是 `qwen` 本地 `_resolve_slide_end_x(...)` 的 image-estimator 覆盖。

## 证据组 B：raw solver 自己就错

附件目录：

- [qwen_visual_probe_after_fix](./artifacts/qwen_solver_audit/qwen_visual_probe_after_fix/)

关键截图：

- [森林 challenge 原图](./artifacts/qwen_solver_audit/qwen_visual_probe_after_fix/challenge_before.png)
- [森林 hold 截图](./artifacts/qwen_solver_audit/qwen_visual_probe_after_fix/challenge_hold.png)

关键事实：

- 这组题图里，真实缺口在中间偏右。
- 但对同一张图离线重复求解 9 次，solver 稳定给出左侧点位，`localGapX` 基本在 `21-26`。
- `hold` 截图里，拼图块几乎没有离开左边，说明浏览器确实按 solver 给的错误点位在拖。

对应元数据：

- [qwen_visual_probe_after_fix/meta.json](./artifacts/qwen_solver_audit/qwen_visual_probe_after_fix/meta.json)

owner 判断：

- 这类失败已经不属于本地纠偏覆盖问题。
- 是 `ohmycaptcha` 对某些 `qwen` 题面的原始 gap 识别本身就偏了。

## 为什么之前会有纠偏

这层逻辑来自 `zai` 的经验：默认假设 LLM / solver 的 gap 可能会漂，而简单图像估计有时能把点位拉回。

但这套假设在 `qwen` 上不稳定：

- 有些题图 `image_estimator` 直接返回 `null`
- 有些题图 `image_estimator` 会把本来更合理的 solver 点位改坏

所以对 `qwen` 而言，这层逻辑不应再覆盖主目标点。

## 本轮代码处理

### 1. 去掉 `qwen` 的 image-estimator 覆盖

现在 `qwen` 的 `_resolve_slide_end_x(...)` 不再用 `image_estimator` 覆盖 solver 主点位，只保留冲突日志。

代码位置：

- [platforms/qwen/core.py](../platforms/qwen/core.py)

### 2. 保留同图多次采样取主簇

因为同一张图重复问 solver 时，gap 会有小幅抖动，所以保留：

- 同图多次求解
- 按本地 `gap x` 聚簇
- 取主簇代表值

代码位置：

- [platforms/qwen/core.py](../platforms/qwen/core.py)

### 3. 已补回归

- [tests/test_qwen_registration.py](../tests/test_qwen_registration.py)

覆盖点：

- 多次采样
- 主簇选择
- image-estimator 与 solver 冲突时保留 solver 点

## 当前状态

已经确认：

- `qwen` 不是纯浏览器拖动问题
- `qwen` 也不是单一 solver 问题
- 当前真实 owner 是“两类失败并存”

拆分如下：

1. `qwen` 本地终点纠偏曾经会把原始点位改坏
2. `ohmycaptcha` 对某些 `qwen` 题图原始识别就会偏左

第一类已经在 repo 侧止血。

第二类仍需在 `ohmycaptcha` 侧做 qwen 专项评估和优化。

## 2026-05-21 模型升级后复测

附件目录：

- [qwen_solver_retest_2026-05-21](./artifacts/qwen_solver_retest_2026-05-21/)

本轮不再混入浏览器拖动，只做两步：

1. 重新抓 3 张新的 `qwen` WAF 题图。
2. 对每张固定题图离线重复调用 `solve_aliyun_slide_action(...)` 9 次。

### 样本 1

题图：

- [sample_1/challenge.png](./artifacts/qwen_solver_retest_2026-05-21/sample_1/challenge.png)

离线分布：

- `localGapX = 185.83 ~ 187.29`
- 9 次结果几乎单峰，波动只有约 `1.5px`

判断：

- 新模型在这张图上已经明显稳定。
- 从视觉上看，缺口就在中间偏右，这个分布是合理的。

### 样本 2

题图：

- [sample_2/challenge.png](./artifacts/qwen_solver_retest_2026-05-21/sample_2/challenge.png)

离线分布：

- `localGapX = 184.58 ~ 186.25`
- 9 次结果同样几乎单峰，波动约 `1.7px`

判断：

- 新模型在这张图上也已经稳定。
- 视觉位置同样与缺口一致。

### 样本 3

题图：

- [sample_3/challenge.png](./artifacts/qwen_solver_retest_2026-05-21/sample_3/challenge.png)

离线分布：

- 一组落在左侧：`18.75 ~ 19.79`
- 一组落在正确右侧：`188.54 ~ 200.0`
- 还有一次中间值：`171.88`

判断：

- 这张图仍然是典型双峰分叉题面。
- 说明新模型提升了整体准确率，但还没有把 `qwen` 全部题型打平。

### 复测结论

新模型带来的变化不是“完全解决”，而是：

1. 对相当一部分普通题图，`ohmycaptcha` 现在已经能稳定给出正确右侧 gap。
2. 但仍然存在少数题图会同时冒出“左侧错点”和“右侧对点”的双峰分叉。

因此更准确的当前判断是：

- `qwen` 本地 image-estimator 覆盖问题已经不该再保留。
- `ohmycaptcha` 准确率确实提升了，但还没有达到“可以认为所有 qwen 题图都稳定正确”的程度。

## 后续建议

建议下一步转到 `ohmycaptcha` 仓库，做真正的 solver 评估，而不是继续在浏览器层乱调轨迹：

1. 固定一组 `qwen` 失败题图库。
2. 对每张图重复求解 10-20 次，统计 `gap x` 分布。
3. 人工按截图标注真实缺口位置。
4. 评估：
   - 平均误差
   - 方差
   - 多峰分布比例
   - 偏左 / 偏右模式
5. 再决定是否需要：
   - qwen 专属 prompt
   - qwen 专属 model candidates
   - 额外的 solver-side confidence / candidate 输出

## 一句话结论

`qwen` 当前的问题，不是简单的“拖得不像人”，而是“目标点存在两类问题：一类曾被本地纠偏改坏，另一类是 solver 原始识别就错”。  
