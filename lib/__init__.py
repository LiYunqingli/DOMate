# -*- coding: utf-8 -*-
"""DOMate 核心库。

模块划分：
    config   —— config.json 读写与默认值
    i18n     —— 语言包加载
    output   —— 终端输出（颜色/表格/JSON）
    errors   —— 错误码
    selector —— 选择器语法解析
    ws       —— 极简 WebSocket 服务端
    daemon   —— HTTP + WebSocket 本地服务
    hub      —— 客户端注册与命令路由
    rpc      —— CLI 侧通讯与自动拉起
    commands —— 按功能分类的命令实现
"""

__version__ = "1.0.0"
