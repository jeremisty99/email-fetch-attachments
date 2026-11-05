# email-fetch-attachments

一份可复用的 Skill / 经验笔记：**只用 Python 标准库，只读接入 IMAP / POP3 邮箱，把邮件正文与附件完整取回本地**。

重点解决一类容易漏掉的文件：QQ 邮箱「超大附件」和网易邮箱「文件中转站 / 云附件」——
它们**不在 MIME 里**，正文只留一条带凭证的下载链接，普通附件解析代码完全拿不到。

## 范围

只包含：**接入 → 拉取 → 解析 → 附件获取**。

不包含：邮件分类、业务归档规则、目录树、内容识别、摘要判定、发送或删除邮件。
把拉取层保持纯粹，后续任何归类逻辑都能接在同一份「原始邮件 + 附件」之上。

## 为什么值得单独记

- `BODY[]` 会顺带把邮件标成已读；只读必须用 `BODY.PEEK[]` + `readonly=True` 的 EXAMINE。
- 部分服务商在 IMAP `login()` 之后要求客户端补发 `ID` 命令，否则 `SELECT` 报
  `NO [EXAMINE Unsafe Login]`；标准库没注册这条命令，要手动注册。
- 网易系网页端默认可能只让第三方客户端看到近 30 天邮件，首次全量拉取前要确认设置。
- 超大附件的链接常藏在 JS 变量或裸文本里，且 `&` 被写成 `&amp;` / `\x26`，
  只抓 `<a href>` 会漏。
- 链接有有效期，解析失败必须只跳过当前项并写标注，不能中断整批。
- 增量同步要用「主指纹 + 备用指纹」两套索引；只下载不分类的旁路必须用自己的水位。

完整流程见 [`SKILL.md`](./SKILL.md)，两家接口细节见 [`references/providers.md`](./references/providers.md)。

## 目录

```
SKILL.md                     技能主文档：协议、只读、去重、解析、附件获取流程
references/providers.md      QQ / 网易超大附件接口、字段回退、错误码
scripts/minimal_fetch.py     纯标准库最小实现，支持 --eml 离线自测
config.example.json          配置字段对照（纯占位，脚本不从这里读凭据）
```

## 快速开始

凭据只从环境变量读取，不写入任何文件：

```bash
export MAIL_ADDRESS="you@example.com"
export MAIL_AUTH_CODE="<客户端授权码，不是登录密码>"
export MAIL_IMAP_HOST="imap.example.com"     # 可选，默认按邮箱域名推断
export MAIL_POP_HOST="pop.example.com"       # 可选

python scripts/minimal_fetch.py --proto imap --limit 10 --out ./out
python scripts/minimal_fetch.py --proto pop3 --limit 10 --out ./out --download-big
```

离线自测（**不需要账号、不联网**，解析一个 `.eml` 并验证 MIME 与链接识别）：

```bash
python scripts/minimal_fetch.py --eml sample.eml --out ./out
```

输出形状：

```
out/
├─ 0001_<邮件主题>/
│  ├─ 正文.txt          头信息 + 纯文本正文 + 附件清单
│  ├─ 正文.html         原始 HTML（链接型附件的依据）
│  └─ 附件/             常规 MIME 附件 + 链接型超大附件
└─ _state.json          增量水位（主/备双指纹）
```

参数：`--proto imap|pop3`、`--limit N`、`--all`（忽略去重全部重拉）、
`--out DIR`、`--eml FILE`、`--download-big`（联网下载链接型附件，默认关闭）。

## 作为 WorkBuddy Skill 使用

把本目录放到 `~/.workbuddy/skills/email-fetch-attachments/` 即可，
`SKILL.md` 的 frontmatter 已按规范写好。也可以直接把仓库 clone 到该路径。

## 隐私与安全

- 本仓库**不含**任何真实邮箱地址、授权码、邮件主题、发件人、附件内容或本机绝对路径；
  示例中的邮箱、域名、key、code 全部为占位符。
- 客户端授权码不是登录密码；真实配置与导出目录请勿提交（`.gitignore` 已忽略常见形态）。
- 原始邮件正文里可能含自授权下载链接，**不要公开真实 `.eml` 或 `正文.html`**。
- 拉取全程只读：不发 `STORE` / `COPY` / `EXPUNGE`，不改服务端已读状态。
- QQ / 网易的相关接口为非公开 Web 接口，随时可能变化；本仓库只用于取回自己的邮件，
  使用前请确认符合服务商条款，并对接口变更做好降级处理。

## 许可

未附带许可证文件；如需开源分发，请自行补充 LICENSE。
