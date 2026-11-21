"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import derived_rule, resource

# 领域专用路由需先于通用资源路由注册，避免被 /api/{module} 动态路径抢先匹配。
ROUTERS = [derived_rule, resource]
