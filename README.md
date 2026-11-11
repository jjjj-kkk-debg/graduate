# 剧匣工程入口

2026-11-04优化版，版本号1.0.0。运行成品：检查环境.bat→启动剧匣.bat；停止用停止剧匣.bat。成品backend/target/juxia-1.0.0.jar已内置前端，运行需Windows x64、JDK17+、完整MySQL8和浏览器；构建另需Node ^20.19或>=22.12、npm、Maven3.9.x。

默认应用8108、数据库3328，冲突另选，以Ready为准。首次空库账号reader/author/author2/reviewer/admin，初始密码123456。目录v5为12部公开作品、2份流程样例、84份教学存档，短篇8章/10选择/3结局/2线索。已有人工内容与发布快照不会无条件覆盖。

私有Key与state在本机.local；数据在公共文档GraduationRuntime独立目录，复制工程不自动带走旧库。不要清库排查普通业务错误，迁移请先备份再明确目标恢复。普通业务无Key可用，模型失败如实提示，无假模型回退。

本轮修改：Editor路由与未保存稿保护、Reader/StoryView请求序号隔离、Library/StoryView辅助错误分开、StoryRepository批量统计阅读次数。详见外层当前验收记录。

源码阅读：frontend/src/main.js→views/components→api.js→backend/src/main/java/cn/juxia/controller/ApiController.java→service→repository→resources/db/migration。

开发者验证：Maven test；Python requests/Playwright见tests/requirements.txt。fresh_catalog_smoke.py使用私有state在独立MySQL库验证并清理；navigation_regression.py模拟写入，不改业务对象或调用付费AI。其他历史验收脚本可能写对象/发模型请求，执行前阅读。2026-11-04证据与历史2026-11-02记录分开，未在目标学生电脑验收。

材料与部署：../02-部署/学生部署说明.md；../04-Word材料/相关材料/。
