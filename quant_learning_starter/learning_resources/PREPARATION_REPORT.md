# 量化资料准备记录

25 项入口已整理；6 份 Notebook、3 份讲义已保存。三本书籍 PDF 尚未取得，四本商业书保留正版入口。

## 实际成功
- 下载 pandas 第 5/6/7/8/10/11 章共六份 Notebook，并保留 COPYING 和 README。
- 上游代码固定在 3rd-edition 分支提交 a892791ce5a3831134cfa2989033ecbb9289f1ea。
- MIT 15.401 Equities（24 页）、Risk and Return（36 页）、MIT 18.05 Covariance and Correlation（10 页）已下载并通过 PDF 解析。
- 下载文件合计 1,136,184 字节；无整套视频、全量仓库或行情数据。
- 原创基础练习已运行：两只虚构股票、八个模拟交易日、16 条记录，五项核对通过。

## 实际失败或只能在线使用
- probability: 作者站跳转的 Google Drive 可阅读 630 页。本轮文件菜单未提供下载，打印也不可用；原下载目标返回网页，未取得本地 PDF。
- islp: 原 PDF URL 实际返回 HTTP 404；作者站当前链接指向 .pdf.download.html，该页在网页工具中返回 403，浏览器未呈现可用下载内容。未改用第三方书籍镜像。
- convex: 官方网页工具能够解析原 PDF（714 页），但本机 Python、curl 和浏览器访问均出现 TLS/连接中断。使用已配置的本地代理作单次下载测试也失败；未更改系统网络设置，未取得本地 PDF。

## 尚未执行
- 四本商业书仅保留出版社/作者入口，未购买或获取正文。
- Python for Data Analysis 只保留作者 HTML 链接，未整站抓取。
- 课程视频只保留入口；其他项目只保存顺序和入口，未安装、未运行。
- 六份上游 Notebook 只校验文件结构，未逐单元执行，未取其全部示例数据与可选依赖。
- 本项目已有合成因子 Notebook 仍在原位置，未改写原 16 周计划。

## 使用
打开 RESOURCE_NAVIGATION.html，可直接进入本地讲义、Notebook 和在线资源。
基础练习命令：

```powershell
D:\Quantification\quant_learning_starter\.venv\Scripts\python.exe D:\Quantification\quant_learning_starter\learning_resources\practice\data_handling.py
```

练习对应 pandas 第 6/8/10/11 章，结果在 practice/output/，明确标记 synthetic。
原包中的历史下载失败声明保存在 source_bundle；此次真实结果见 books/download_status.json、starter_download_status.json、book_access_review.json 和 preparation_status.json。
教材和讲义按原作者/机构的使用条款仅供个人学习备查，不随项目代码提交；许可证说明见 LICENSE_NOTES.md。
