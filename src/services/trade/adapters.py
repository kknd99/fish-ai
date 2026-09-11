"""交易执行适配器。

这一层是唯一允许产生"资金后果"的地方，因此：

1. 默认适配器是 :class:`DryRunAdapter`，什么都不做；
2. :class:`NotifyLinkAdapter` 只发送**真实商品链接**与操作指引，由人来完成下单；
3. 真正的自动提交（:class:`PlaywrightCheckoutAdapter`）**故意留空**——它需要先
   对着真实站点把下单流程逆向清楚，不能在没验证过的前提下用猜的 URL 假装能下单。

关于"一键下单链接"的坑（务必先读再动手）：
上游存在一个未合并的 PR #474「feat auto order on price match」，它的核心是
``OrderLinkService.generate_order_link()``，拼接

    https://www.goofish.com/order/confirm?itemId=<id>&price=<price>&source=monitor

这类链接是**推测出来的**：闲鱼并不支持用 URL 参数驱动下单，价格由服务端决定，
该路径大概率不存在或直接跳回商品页。该 PR 自己的文档也写着"自动购买：实验性功能，
暂未完全实现"，且 ``generate_direct_buy_link()`` 的实现就是原样返回商品链接并注释
"闲鱼不支持通过 URL 参数直接跳转到支付页面"。PR 自 2026-04-20 起未再更新。
结论：可以参考它的**配置字段设计**（auto_order_enabled / target_price / action），
但不要复用它的链接生成逻辑，否则会做出一个"看起来能下单、实际下不了"的功能。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from src.services.trade.models import TradeIntent


@dataclass(frozen=True)
class AdapterResult:
    """适配器执行结果。"""

    ok: bool
    detail: str = ""
    external_ref: Optional[str] = None


class TradeAdapter(Protocol):
    """交易适配器接口。"""

    name: str

    async def submit(self, intent: TradeIntent) -> AdapterResult:
        ...


class DryRunAdapter:
    """演练适配器：只回执，不产生任何外部动作。"""

    name = "dry_run"

    async def submit(self, intent: TradeIntent) -> AdapterResult:
        return AdapterResult(
            ok=True,
            detail=(
                f"[dry-run] 本应下单: {intent.title} @ {intent.price} "
                f"({intent.link}) — 未提交任何订单"
            ),
            external_ref=None,
        )


class NotifyLinkAdapter:
    """半自动适配器：把商品与操作指引推给人工确认。

    不伪造下单 URL（见模块文档），只发真实商品链接。这是当前最稳妥的"抢时间"
    方案：省掉的是"盯着页面刷"的时间，不是"确认付款"的动作。
    """

    name = "notify_link"

    def __init__(self, notification_service=None) -> None:
        self._notification_service = notification_service

    def _resolve_service(self):
        if self._notification_service is not None:
            return self._notification_service
        from src.services.notification_service import build_notification_service

        return build_notification_service()

    async def submit(self, intent: TradeIntent) -> AdapterResult:
        service = self._resolve_service()
        if not getattr(service, "clients", None):
            return AdapterResult(
                ok=False,
                detail="未配置任何通知渠道，无法把待购商品推送出去",
            )
        reason = (
            "价格与判定条件命中，请人工确认后下单。\n"
            f"商品: {intent.title}\n"
            f"价格: {intent.price}\n"
            f"卖家: {intent.seller or '未知'}\n"
            f"链接: {intent.link}\n"
            "（本系统不会自动提交订单，也不会自动付款。）"
        )
        results = await service.send_notification(intent.notification_payload(), reason)
        failed = [name for name, item in results.items() if not item.get("success")]
        if failed and len(failed) == len(results):
            return AdapterResult(ok=False, detail=f"所有通知渠道均发送失败: {failed}")
        return AdapterResult(
            ok=True,
            detail=f"已推送人工确认请求到: {', '.join(results.keys()) or '无'}",
            external_ref=None,
        )


class PlaywrightCheckoutAdapter:
    """真实下单适配器——**尚未实现**，故意如此。

    要落地它，必须先完成（不要在没验证前写代码）：
    1. 用 Playwright 对着自己的账号手动跑一遍 H5 下单流程，录下每一步的请求，
       确认真实的提交接口与必需字段（含收货地址 ID、下单 token/风控参数）；
    2. 确认幂等语义：重复提交同一商品会发生什么；
    3. 确认**支付环节无法也不应自动化**——支付需要密码/生物识别，且属平台风控
       最敏感的路径。合理边界是"提交到待付款"，然后把待付款链接推给人；
    4. 评估账号风险：下单是比搜索敏感得多的动作，本项目在上游 issue 里已经长期
       受 FAIL_SYS_USER_VALIDATE / baxia 风控困扰（#505/#492/#473/#465/#472）。
    """

    name = "playwright_checkout"

    async def submit(self, intent: TradeIntent) -> AdapterResult:
        raise NotImplementedError(
            "PlaywrightCheckoutAdapter 尚未实现：需先逆向并验证真实下单流程，"
            "详见该类文档字符串。当前请使用 notify_link 或 dry_run。"
        )


ADAPTER_REGISTRY: dict[str, type] = {
    DryRunAdapter.name: DryRunAdapter,
    NotifyLinkAdapter.name: NotifyLinkAdapter,
    PlaywrightCheckoutAdapter.name: PlaywrightCheckoutAdapter,
}


def build_adapter(name: str, *, notification_service=None) -> TradeAdapter:
    """按名字构造适配器；未知名字一律降级为 dry-run（fail-safe）。"""
    key = (name or "").strip().lower()
    if key == NotifyLinkAdapter.name:
        return NotifyLinkAdapter(notification_service=notification_service)
    if key == PlaywrightCheckoutAdapter.name:
        return PlaywrightCheckoutAdapter()
    return DryRunAdapter()
