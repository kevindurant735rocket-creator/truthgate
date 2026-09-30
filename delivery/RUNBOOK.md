# RUNBOOK

启动：`PORT={{PORT}} python3 outputs/bundle/app.py`
回滚：bundle 整目录复制即备份（ship 自动留 .prev）。
健康检查：GET / 200 且长度>50B（见 public_root_200）。
