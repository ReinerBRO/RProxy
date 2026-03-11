#!/bin/bash
# 备份数据库
cp data/one-api.db data/one-api.db.backup

# 获取当前的 ModelRatio 配置
CURRENT_RATIO=$(sqlite3 data/one-api.db "SELECT value FROM options WHERE key='ModelRatio';" 2>/dev/null)

if [ -z "$CURRENT_RATIO" ]; then
    # 如果没有配置，创建新的
    NEW_RATIO='{"gpt-4":15,"gpt-4-turbo":10,"gpt-3.5-turbo":0.5,"gpt-4o":15,"gpt-5.3-codex":30,"gpt-5.4":30}'
    sqlite3 data/one-api.db "INSERT INTO options (key, value) VALUES ('ModelRatio', '$NEW_RATIO');"
else
    # 如果已有配置，添加 gpt-5.4
    echo "Current ratio: $CURRENT_RATIO"
    # 使用 jq 添加 gpt-5.4
    NEW_RATIO=$(echo "$CURRENT_RATIO" | jq '. + {"gpt-5.4": 30}')
    sqlite3 data/one-api.db "UPDATE options SET value='$NEW_RATIO' WHERE key='ModelRatio';"
fi

echo "Updated ModelRatio configuration"
