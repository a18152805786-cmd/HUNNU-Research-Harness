from __future__ import annotations

from typing import Any

from ..auth.state import classify_auth_state
from ..models import AuthStatus, BrowserState, DownloadRequest
from .base import DatabaseAdapter, UnexpectedPageState


class CNRDSAdapter(DatabaseAdapter):
    name = "CNRDS"
    cnfs_path = ("基础库", "上市公司财务基础数据", "财务报表（CNFS）")
    tables = ("资产负债表", "利润表", "现金流量表")

    async def detect(self) -> BrowserState:
        state = await self.browser.state(database=self.name)
        detected_auth = classify_auth_state(state)
        return BrowserState(**{**state.__dict__, "database": self.name, "auth_status": detected_auth})

    async def _require_cnrds(self, *, module: str | None = None, table: str | None = None) -> BrowserState:
        state = await self.detect()
        haystack = f"{state.url} {state.title} {state.body_text}".lower()
        if "cnrds" not in haystack and "中国研究数据服务平台" not in haystack:
            raise UnexpectedPageState("UnexpectedPageState: current page is not identified as CNRDS.")
        if state.auth_status in {AuthStatus.AUTH_REQUIRED, AuthStatus.AUTH_IN_PROGRESS, AuthStatus.SESSION_EXPIRED}:
            raise UnexpectedPageState("ManualLoginRequired=true; CNRDS authentication is not complete.")
        return BrowserState(**{**state.__dict__, "module": module or state.module, "table": table or state.table})

    async def open(self) -> BrowserState:
        await self._require_cnrds()
        for label in self.cnfs_path:
            await self.browser.click_text(label)
        return await self._require_cnrds(module="CNFS")

    async def open_table(self, table: str) -> BrowserState:
        if table not in self.tables:
            raise ValueError(f"Unsupported CNRDS CNFS table: {table}")
        await self._require_cnrds(module="CNFS")
        await self.browser.click_text(table)
        return await self._require_cnrds(module="CNFS", table=table)

    async def open_balance_sheet(self) -> BrowserState:
        return await self.open_table("资产负债表")

    async def open_income_statement(self) -> BrowserState:
        return await self.open_table("利润表")

    async def open_cashflow_statement(self) -> BrowserState:
        return await self.open_table("现金流量表")

    async def select_stocks(self, stocks: list[str]) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        try:
            await self.browser.select_options_by_label("股票代码", stocks)
        except Exception as exc:
            if len(stocks) != 1:
                raise UnexpectedPageState("CNRDS stock selector is not a native multi-select; selector validation is required before selecting multiple stocks.") from exc
            await self.browser.fill_by_label("股票代码", stocks[0])
        return state

    async def select_date_range(self, start: str, end: str) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        await self.browser.fill_by_label("开始日期", start)
        await self.browser.fill_by_label("结束日期", end)
        return state

    async def select_fields(self, fields: list[str]) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        for field in fields:
            locator = self.browser.page.get_by_label(field, exact=False).first
            await locator.check()
        return state

    async def preview(self) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        await self.browser.click_text("预览")
        return await self._require_cnrds(module="CNFS", table=state.table)

    async def download(self, request: DownloadRequest) -> Any:
        state = await self._require_cnrds(module="CNFS", table=request.table)
        if state.table != request.table:
            raise UnexpectedPageState(f"UnexpectedPageState: expected table={request.table}, observed={state.table}")
        await self.browser.click_text("下载")
        return await self.browser.download_by_text("确认下载")
