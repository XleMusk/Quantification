# 量化学习资源：按顺序使用

这是本轮生成的排序清单、网页导航和官方下载脚本，**不含书籍正文，不是电子书全集**。

## 先打开什么

- RESOURCE_NAVIGATION.html：在浏览器打开，直接进入书籍、课程、项目。
- READING_ORDER.md：详细顺序、阅读主题、项目练习入口。
- CODEX_PREPARE.md：交给Codex的本轮任务。

## 电子书现状

三本有作者提供的公开PDF入口：Introduction to Probability、ISLP、Convex Optimization。
Python for Data Analysis作者提供免费HTML版。
其余四本为商业书，只保留出版社或作者入口。
本轮容器网络/下载工具未能取得PDF，因此没有附PDF文件。

## 在有网络的云端重新下载

需要Python 3.10+，不需要第三方库：

```bash
python download_open_books.py --dry-run
python download_open_books.py --out /your/cloud/reading/books
```

也可选择一本：

```bash
python download_open_books.py --only convex --out ./books
```

脚本不绕过登录、付费或下载确认。遇到网页而非PDF时会报告失败，并给出官方入口。下载状态写入download_status.json；本轮真实尝试记录另存initial_download_status.json。

## 已完成的验证

清单结构、脚本语法和离线模拟响应的PDF判别逻辑通过。没有成功完成远程PDF下载；没有安装或运行量化项目；没有下载课程视频或行情数据。
