# 4.3.3a 车站显示调整

日期：2026-09-15

发布状态：实现、构建与本地提交已完成；`origin/main` 仍为 4.3.2a。生产推送与 `v4.3.3a` 标签未执行，等待用户明确授权后再触发 Cloudflare Pages 部署。

## 需求与落实

- 车站显示提前一级：z12（东京比例尺 3 km）显示 72 个三级枢纽；z13（1 km）显示 607 个二、三级站；z14（500 m）起显示全部 8,954 站。z11 不请求车站载荷。
- z12–14 维持 11 px 图标，z15 起维持 11／14／18 px，新增密度没有同时放大图标。
- 标签预计算范围从 z14–19 扩为 z12–19，并使用与图标相同的分级候选。
- 移除车站点击、悬停命中检测及临时 Tooltip。Canvas 固定站名保留，是唯一的站名呈现。
- 图层菜单顺序改为“车站／长途／市内／景点／书签”。铁路开启时强制保留车站的原规则不变。

## 验证

- `scripts/verify_build.py`：通过；版本 4.3.3a，载荷 8,954 行、gzip 201.2 KiB。
- `tests/transit/station_payload_test.py`：4/4 通过。
- `tests/ux/station_visibility_433.py`：通过。实测可见总数 72／607／8,954，比例尺 3 km／1 km／500 m；临时 Tooltip 0，点击与悬停监听均不存在；菜单顺序符合要求。
- 实景截图：[z12](station-z12.png)、[z13](station-z13.png)、[z14](station-z14.png)。

## 指纹与边界

- `docs/transit-layer.js` SHA-256：`ef8fe81bfb824cc5b35829e12344e8e7738e1ce7959bd4944bdcc68f6f50ef84`；页面 MD5 版本：`a533d687bf`。
- 车站载荷：`docs/data/stations.a6e8cb0c23ce.json`，完整 SHA-256：`a6e8cb0c23ce647dfd8efec7fb8275d3300f86c6dc6799c915b40f34f85af1fc`。
- `docs/index.html` SHA-256：`f377470c96e6596adfff70020be6a39b6038493fa391e136eaaa556475c22c5d`。
- 没有 Fold 真机性能结论；本轮按用户要求只做针对性浏览器验证。
