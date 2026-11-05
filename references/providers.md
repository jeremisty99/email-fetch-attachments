# 链接型超大附件：QQ 与网易的解析细节

> 本文只描述「从邮件正文里的下载链接取回文件」这一环节。
> 所有 `host / key / code / linkKey` 均为占位符；接口为非公开 Web 接口，随时可能变化。

## 共同前提

- 邮件里出现的是**取件页 / 下载页链接**，不是文件直链；文件本体不在 MIME 里。
- 链接自带一次性凭证（QQ 的 `key` / `code`，网易的 `file=` 或 `key=`），
  因此通常无需登录态，但**有有效期**；过期的链接无法补救。
- 用带 `CookieJar` 的 `urllib` opener，带常规 `User-Agent` / `Referer`，允许跟随重定向。
- 每个字段都要有回退：接口改版、字段改名、HTML 结构变化时应降级而不是抛异常中断整批。

## 链接提取（两家共用）

从 HTML 正文里按以下顺序收集 URL，去重且保持出现顺序：

1. `href="..." / href='...'`；
2. JS / 属性里的 `var url = "..."`、`downloadlink="..."`、`download_url`、`downloadUrl`；
3. 剩余裸文本 `https?://...`。

每一条再依次处理：

- `html.unescape()` 还原 `&amp;` 等实体，否则 `urlparse` 会在 `&amp;` 处截断 query；
- 把 `\x26` / `\u0026` 还原成 `&`（旧版取件页把直链放在 JS 字符串里时会这样转义）；
- 去掉尾部标点 `.,;:、。）】》"'`；
- 若该 URL 是已收集链接的前缀，丢弃（JS 完整版在前，裸文本正则常只匹配到被截断的前半段）。

## QQ 邮箱：两条解析路线

### 路线 A —— `QQMailBigAttach` 块（信息最全，优先）

1. 按 `class="bigatt_bt"` 切出每个附件块；块结构异常时退化为「整段当一项」。
2. 从块内 `downloadlink="..."` 取 query 中的 `key` 与 `code`；缺任一则跳过该项。
3. 文件名优先取 `<a style="color:#000;">名称</a>`，其次 `title="..."` 的首行，
   最后用占位名（下载后由响应头纠正）。
4. 用 `key/code` 换直链：

   ```
   POST https://wx.mail.qq.com/ftn/download
   Content-Type: application/x-www-form-urlencoded
   Referer: https://wx.mail.qq.com/

   func=3&key=<KEY>&code=<CODE>&f=json
   ```

   响应取 `body.url`（回退 `data.url`）作为真实下载地址；
   `head.ret` 非 0 视为失败，记录 `head.msg` / `head.stack` 便于排查。

5. 指纹用 `qq:<key>:<code>`，用于跨邮件、跨批次去重。

### 路线 B —— 裸取件链接兜底

识别 `wx.mail.qq.com/ftn/download`、`mail.qq.com/cgi-bin/ftnexs_download` 等取件链接，
直接 GET 该取件页，按响应形态二选一：

- **新版 JSON**（`Content-Type` 含 `json` 或响应体以 `{` 开头）：
  取 `body.url`，依次回退 `body.download_url`、`fileUrl`、`file_url`、`downloadUrl`、`link`；
  文件名可读 `body.filename / fileName / name`；`head.ret` 非 0 为失败。
- **旧版 HTML**（脚本里内嵌直链）：正则 `var\s+url\s*=\s*"([^"]+)"` 取直链。

两种形态都要把 `\x26` 还原成 `&`。此路线通常拿不到文件名，交给下载阶段的
`Content-Disposition` 兜底。指纹有 `key/code` 时用 `qq:<key>:<code>`，
没有则用 `qq:url:<链接短哈希>`。

> 路线 A 已处理过的 `(key, code)` 要在路线 B 中跳过，否则同一文件会被下载两遍。

## 网易邮箱（163 / 126 / yeah.net / 188.com）

### 1. 提取链接

- 域名白名单：仅当 host 等于或属于上述后缀才处理，避免把任意外链喂给内部接口。
- query 里取 `file=`（文件中转站 / 超大附件）或 `key=`（邮箱大师云附件），
  两者统一记作 `linkKey`，下游无需区分来源。

### 2. 取文件名（失败不致命）

```
GET https://<host>/filehub/bg/link/info/get?key=<urlencode(linkKey)>
Accept: */*
```

`code == 200` 时从 `data.linkInfo.fileInfo.filename` 取名字。
此步失败**不要放弃整条链接**，继续下一步并使用占位名，最后由响应头纠正。

### 3. 换真实下载地址

```
POST https://<host>/filehub/bg/dl/prepare
Content-Type: application/json

{"linkKey": "<linkKey>"}
```

响应取 `data.downloadUrl`（回退顶层 `downloadUrl`）。没有地址时按错误码给提示：
`code == 601` 或 `desc == EXPIRED` → 附件已过期；`desc == PARAM_ILLEGAL` → 链接无效/失效；
`code == 0` 且无地址 → 接口未返回下载地址。

### 4. 下载

`GET <downloadUrl>` 流式落盘。指纹用 `netease:<host>:<linkKey>`。

### 5. 关键字兜底标注

正文中出现「文件中转站」「网易网盘」「云附件」「超大附件」但没解析出可用链接时，
仍应写「未拉取」标注提醒人工。注意排除误判：

- 正文含 `QQMailBigAttach` 或只有 QQ 取件链接时，不要把这些字样当成网易附件；
- 只有「超大附件」四个字、没有任何链接时，同样只标注、不尝试请求。

> 该组接口思路参考社区项目 Y-Mail 对网易文件中转站的逆向实现。

## 通用下载与去重细节

- `Content-Disposition` 解析顺序：`filename*=UTF-8''<urlencoded>` → `filename="..."` → URL 路径末段；
  三种都取不到才保留占位名。
- 目标名已存在则加 `(2)`、`(3)` 后缀，绝不覆盖；下载到 0 字节要删除文件并计为失败。
- 缓存命中后先比 size、再比 sha256；若命中文件就在本次目标目录里，直接认账，不再复制一份。
- `skip_existing` 语义：补跑历史邮件时，目标目录里已有同名非空文件就跳过下载，
  避免生成 `xxx(2).zip` 这类重复副本。
- 「发现过候选链接」与「成功下载 0 个」要分开返回布尔值：
  只有前者为真、后者为零时才写标注；压根没有链接时不该产生噪音。

## 失败处理的分层

1. 单条链接解析/下载失败 → 记日志、跳过，继续同封邮件的其它附件；
2. 某封邮件的超大附件全部失败 → 写标注文件，并在纯文本正文末尾追加一行提示；
3. 某封邮件解析异常 → 跳过该封，继续下一封；
4. 网络整体不可用 → 保留已完成的邮件，水位只记录真正落盘的部分，下次可断点续跑。
