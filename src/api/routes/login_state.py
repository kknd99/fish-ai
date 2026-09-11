"""
登录状态管理路由
"""
import os
import json
import aiofiles
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.secure_files import restrict_file


router = APIRouter(prefix="/api/login-state", tags=["login-state"])


class LoginStateUpdate(BaseModel):
    """登录状态更新模型"""
    content: str


@router.post("", response_model=dict)
async def update_login_state(
    data: LoginStateUpdate,
):
    """接收前端发送的登录状态JSON字符串，并保存到 xianyu_state.json"""
    state_file = "xianyu_state.json"

    try:
        # 验证是否是有效的JSON
        json.loads(data.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="提供的内容不是有效的JSON格式。")

    try:
        # 该文件是完整的闲鱼会话 cookie，必须以 0600 落盘（此前是默认的 0644，
        # 同机其他用户/共享卷容器可直接读走）。
        async with aiofiles.open(state_file, 'w', encoding='utf-8') as f:
            await f.write(data.content)
        restrict_file(state_file)
        return {"message": f"登录状态文件 '{state_file}' 已成功更新。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入登录状态文件时出错: {e}")


@router.delete("", response_model=dict)
async def delete_login_state():
    """删除 xianyu_state.json 文件"""
    state_file = "xianyu_state.json"

    if os.path.exists(state_file):
        try:
            os.remove(state_file)
            return {"message": "登录状态文件已成功删除。"}
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"删除登录状态文件时出错: {e}")

    return {"message": "登录状态文件不存在，无需删除。"}
