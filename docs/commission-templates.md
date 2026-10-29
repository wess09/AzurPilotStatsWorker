# 委托事件与未知物品模板协议

本适配器负责 A → B → C 流水线的 B 节点。A 按上传组和文件索引提供有序原图；B 识别并持久化结果；C（`AzurPillotCloudServer`）负责全局未知身份、审核发布和统计。传输批次可以混合多个设备、用户和场景，配对状态只存在于当前图片组。

## 委托输出

既有结果信封和修订回执不变，解析器版本为 `azurpilot-adapter-v4`。委托事件示例：

```json
{
  "record_id": "成功页位置与源组哈希生成的64位SHA256",
  "event_type": "drop",
  "series": 0,
  "focus_series": 0,
  "project": "初级林木护卫委托",
  "name": "初级林木护卫委托",
  "comm": "Forest Protection Commission Ⅰ",
  "status": 0,
  "duration_seconds": 1800,
  "items": [{"item": "Coins", "amount": 28, "tag": ""}]
}
```

`status=0/1` 分别表示成功／大成功。活动委托未收录英文代码时 `comm:null`、时长 0。零奖励使用 `PlaceHolder:1`；多个收获页使该条无效。名称、数量、图像或配对错误使整组待复核，不能仅凭同组另一条成功记录进入统计。

未知槽位另带 `unresolved_items`，每项为 `{local_key, candidate_id?, amount, tag}`；`local_key` 是 49×49 RGB 像素的 SHA-256。已获 C 确认的相同图标省略重复候选，只保留哈希引用。未映射的 U 身份不能作为正式物品统计；C 仅在整组所有诊断都由未知物品引起、且全部行已解析时恢复统计，其他失败仍需重跑或复核。

## 未知物品闭环

1. B 裁剪图标，向既有 `/internal/v1/template-candidates` 发送候选。候选 ID 保持 PNG 字节摘要以兼容旧校验，像素摘要放在 `metadata.icon_sha256`。
2. C 先查全局像素哈希，必要时调用 B 的 `/internal/v1/icon-matches`。一次 1–50 张图，请求最多 32 MiB；B 使用有界原生进程池匹配。
3. 回执 `{accepted,id,match,item?|key?,local_key,revision,need_screenshot}` 中，`match` 为 `formal/cluster/new`。正式名直接使用，未知身份只由 C 分配。B 保存已确认图标的有界本地缓存。
4. 结果被 C 接收后，若 `need_screenshot=true`，B 向 `/internal/v1/unknown-screenshots` 登记 `{key,candidate_id,sample_sha256,file_index,frame_index}`。原图已经在 123 盘，这里登记按需预览引用，不重复上传完整图片。每簇最多三个有效引用，90 天过期，分类／忽略后清除引用。节点不会删除 123 盘原图。
5. 人工分类、合并或拆分后，C 以新版本发布映射和模板。历史未解析行通过映射与别名查询得到物品；识别内容或规则变化会重跑受影响记录。

L1 使用 DCT64 pHash（灰度、INTER_AREA 缩至 32×32、DCT 左上 8×8、中位数阈值、按行打包），默认汉明距离 ≤6、均色差 ≤30。L2 为 49×49 对 49×49 的 `cv2.TM_CCOEFF_NORMED`，正式 ≥0.92、未知簇 ≥0.96、不同规范身份间隔 ≥0.03；同物品 `_数字` 变体不占用次优名额。空间纹理标准差 <5 时改用 `1-RMSE/255`。0.85 以上的疑似正式名仅供人工参考。

## 签名与版本

C 的 `TEMPLATE_SIGNING_KEY` 为 Base64 编码的 Ed25519 32 字节种子（也接受一致的 64 字节私钥）；B 的 `TEMPLATE_PUBLIC_KEY` 为对应的 Base64 原始 32 字节公钥，应由管理员独立配置。B 不信任响应里临时提供的公钥。

完整包仍通过 `/internal/v1/template-releases/{id}` 下载，请求包含 `Accept: application/vnd.azurpilot.template-bundle+json;version=1`；不带该能力标识的旧 B 只收到正式模板数组。新包字段为：

```text
schema_version=1, revision, templates[], mappings{}, aliases{},
hash_overrides{}, match_thresholds{}
```

模板含 `id/item_id/scene/enabled/kind/image/sha256/icon_sha256`；只接受有界 RGB PNG，49×49 图标或可裁剪的 96×96 物品格。C 发布时规范化为 49×49。临时资源仅在至少五个不同设备提供观察后分发，身份跨场景共用。

公开 `/api/v3/get/templates/manifest` 支持 ETag；`/api/v3/get/templates/delta?from=<revision>` 返回完整或增量包。增量带 `base_revision`、模板 `deleted` 墓碑，以及 `deleted_mappings/deleted_aliases/deleted_hash_overrides`。B 在已有签名快照时优先取增量，版本不符或基础版本缺失则取指定版本完整包。修改阈值必须发布新版本。

响应头 `X-Content-SHA256` 校验原始 JSON 字节。Ed25519 的签名消息为 UTF-8/ASCII：

```text
azurpilot-templates-v1\n<十进制revision>\n<原始响应字节的SHA256小写十六进制>
```

签名通过 `X-Content-Signature` 提供 Base64 字符串。验签、文件摘要、类型和别名检查全部通过后，B 在一个 SQLite 事务中替换有效资源及版本。损坏、过期、低版本或基础版本不符的包不改变当前快照。回滚由 C 复制旧内容生成更大的新版本。轮换签名密钥后，应重新发布活动内容并同步 B 的固定公钥。

## 升级顺序

先升级 C 并执行新迁移，再配置 C → B 的 `RECOGNIZER_URL/WORKER_TOKEN` 和成对签名密钥，发布一次资源版本，然后升级 B。旧记录允许缺少新增维度；旧客户端仍可下载正式模板数组。配置固定公钥的 B 会拒绝未签名内容。两个密钥留空仅用于原协议兼容，公开 v3 资源接口需要签名配置。

测试只使用专用数据库和本地原图副本，禁止对生产库运行验收。玩家原图、数据库、密钥和本地验证脚本均不提交。
