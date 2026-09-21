# 网站版本快照与 SEO 对比工具

本地化网站版本管理：每次快照完整保存网站的 HTML、SEO 数据与截图（git 版本化），
发版前后对比两版差异，输出分级中文报告。**不依赖 Wayback 等外部存档服务。**

## 快速开始

```bash
# 1. 首次：为当前线上版本建立基线（自动编号 v1，git 提交并打标签）
python3 sitediff.py snapshot https://your-site.com/ --commit

# 2. 发版后：抓取新版（自动编号 v2）
python3 sitediff.py snapshot https://your-site.com/ --commit

# 3. 对比最近两个版本，生成中文报告
python3 sitediff.py compare --auto
```

报告输出至 `reports/v1-v2.md`，含变化总览、SEO 变化（🔴高影响/🟡中/🟢低）、
网页文本 diff、截图并排对比与修复建议。

## 常用参数

| 命令 | 参数 | 说明 |
|---|---|---|
| snapshot | `--max-pages N` | 最多抓取页面数（默认 10，含 sitemap 抽样） |
| snapshot | `--pages /about /pricing` | 额外指定重点页面 |
| snapshot | `--no-sitemap` | 不从 sitemap 抽样 |
| snapshot | `--no-screenshot` | 跳过截图 |
| snapshot | `--commit` | 快照后自动 git 提交并打标签 vN |
| compare | `v1 v2` 或 `--auto` | 指定版本或自动取最近两版 |
| compare | `--output FILE` | 报告输出路径 |

## 每次快照保存什么

```
snapshots/v1/
  pages/*.html        # 各页面原始 HTML
  screenshots/*.png   # 无头 Chrome 渲染截图（1280×2400）
  seo.json            # 结构化 SEO 数据（对比用的核心文件）
  robots.txt          # 如存在
  sitemap.xml         # 如存在
  meta.json           # 抓取元信息（时间/状态/备注）
reports/v1-v2.md      # 版本对比报告
```

## 检测维度

**SEO**：title、meta description、canonical、meta robots（noindex/nofollow）、
hreflang、`<html lang>`、Open Graph、Twitter Cards、JSON-LD 结构化数据、
H1–H6 层级、图片 alt 覆盖率、内/外链集合、robots.txt、sitemap.xml。

**网页**：可见文本 diff、标题/链接/图片结构变化、正文文字量、截图视觉对比。

## 约定与限制

- 内链判定采用严格同 host（www 与裸域名视为不同主机）
- JS 动态渲染内容以无头 Chrome 截图为准；HTML 解析基于服务端返回的源码
- 需要登录/内网的站点暂不支持（需匿名可访问）
- 零第三方依赖，仅需 Python 3 标准库；截图需要本机安装 Chrome/Chromium
