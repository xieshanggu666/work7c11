import uvicorn
import os

# 默认 8001：避免与用户其他占用 8000 的后端服务（如三维网格修复平台）冲突，
# 可用环境变量 CARD_GAME_PORT 覆盖。
if __name__ == "__main__":
    port = int(os.environ.get("CARD_GAME_PORT", "8001"))
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=True)