"""
WebSocket 路由
提供实时通信功能
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set

from src.api.auth import websocket_is_authenticated


router = APIRouter()

# 全局 WebSocket 连接管理
active_connections: Set[WebSocket] = set()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    """WebSocket 端点（握手阶段校验会话 cookie）。"""
    # 未认证直接拒绝：WebSocket 不经过 HTTP 中间件，必须在这里单独校验。
    if not websocket_is_authenticated(websocket):
        await websocket.close(code=1008)
        return

    # 接受连接
    await websocket.accept()
    active_connections.add(websocket)

    try:
        # 保持连接并接收消息
        while True:
            # 接收客户端消息（如果有的话）
            data = await websocket.receive_text()
            # 这里可以处理客户端发送的消息
            # 目前我们主要用于服务端推送，所以暂时不处理
    except WebSocketDisconnect:
        active_connections.discard(websocket)
    except Exception as e:
        print(f"WebSocket 错误: {e}")
        active_connections.discard(websocket)


async def broadcast_message(message_type: str, data: dict):
    """向所有连接的客户端广播消息"""
    message = {
        "type": message_type,
        "data": data
    }

    # 移除已断开的连接
    disconnected = set()

    # 注意：必须遍历快照。本函数在 await 期间会让出事件循环，
    # 而端点可能在此时从 active_connections 里移除连接（或新增），
    # 直接遍历实时集合会抛 "Set changed size during iteration"，
    # 该异常会沿着生命周期钩子逃逸并导致任务状态不再同步。
    for connection in list(active_connections):
        try:
            await connection.send_json(message)
        except Exception:
            disconnected.add(connection)

    # 清理断开的连接
    for connection in disconnected:
        active_connections.discard(connection)
