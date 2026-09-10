"""Ferramentas de escrita (mutate) para o fork do Google Ads MCP.

Todas as ferramentas rodam em dry_run=True por padrão: a API valida a
operação (validate_only) sem aplicar nada. Só executa de verdade quando
o agente chama novamente com dry_run=False, depois da aprovação humana.
"""

from typing import Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

write_mcp = FastMCP("write")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _service(client, name: str):
    return client.get_service(name, interceptors=[MCPHeaderInterceptor()])


def _raise(ex: GoogleAdsException):
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(msgs))


def _log(tool: str, customer_id: str, dry_run: bool, detail: Dict[str, Any]):
    utils.logger.info(
        f"ads_mcp.write {tool} customer={customer_id} "
        f"dry_run={dry_run} {detail}"
    )


@write_mcp.tool(annotations=_WRITE)
def set_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: Literal["PAUSED", "ENABLED"],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Pausa ou ativa uma campanha.

    Sempre chame primeiro com dry_run=True e peça aprovação ao usuário
    antes de repetir a chamada com dry_run=False.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "CampaignService")

    op = client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = svc.campaign_path(customer_id, campaign_id)
    campaign.status = client.enums.CampaignStatusEnum[status]
    client.copy_from(
        op.update_mask, protobuf_helpers.field_mask(None, campaign._pb)
    )

    req = client.get_type("MutateCampaignsRequest")
    req.customer_id = customer_id
    req.operations.append(op)
    req.validate_only = dry_run

    try:
        svc.mutate_campaigns(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    detail = {"campaign_id": campaign_id, "new_status": status}
    _log("set_campaign_status", customer_id, dry_run, detail)
    return {"dry_run": dry_run, "applied": not dry_run, **detail}


@write_mcp.tool(annotations=_WRITE)
def update_campaign_budget(
    customer_id: str,
    campaign_id: str,
    new_daily_budget: float,
    max_change_pct: float = 30.0,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Altera o orçamento diário de uma campanha (valor na moeda da conta).

    Recusa variações acima de max_change_pct em relação ao orçamento atual
    e orçamentos compartilhados (afetariam outras campanhas).
    Sempre chame primeiro com dry_run=True.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    ga = _service(client, "GoogleAdsService")

    query = (
        "SELECT campaign.campaign_budget, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}"
    )
    try:
        rows = list(ga.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise(ex)
    if not rows:
        raise ToolError(f"Campanha {campaign_id} não encontrada.")

    row = rows[0]
    if row.campaign_budget.explicitly_shared:
        raise ToolError("Orçamento compartilhado: altere manualmente na interface.")

    current = row.campaign_budget.amount_micros / 1_000_000
    change_pct = abs(new_daily_budget - current) / current * 100 if current else 100
    if change_pct > max_change_pct:
        raise ToolError(
            f"Variação de {change_pct:.1f}% excede o limite de "
            f"{max_change_pct:.0f}% (atual: {current:.2f})."
        )

    svc = _service(client, "CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    budget = op.update
    budget.resource_name = row.campaign.campaign_budget
    budget.amount_micros = int(round(new_daily_budget * 1_000_000))
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, budget._pb))

    req = client.get_type("MutateCampaignBudgetsRequest")
    req.customer_id = customer_id
    req.operations.append(op)
    req.validate_only = dry_run

    try:
        svc.mutate_campaign_budgets(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    detail = {
        "campaign_id": campaign_id,
        "budget_before": current,
        "budget_after": new_daily_budget,
        "change_pct": round(change_pct, 1),
    }
    _log("update_campaign_budget", customer_id, dry_run, detail)
    return {"dry_run": dry_run, "applied": not dry_run, **detail}


@write_mcp.tool(annotations=_WRITE)
def add_campaign_negative_keywords(
    customer_id: str,
    campaign_id: str,
    keywords: List[str],
    match_type: Literal["EXACT", "PHRASE", "BROAD"] = "PHRASE",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Adiciona palavras-chave negativas no nível da campanha.

    Cada palavra consome 1 operação da cota diária. Máximo de 200 por
    chamada. Sempre chame primeiro com dry_run=True.
    """
    if len(keywords) > 200:
        raise ToolError("Máximo de 200 negativas por chamada.")

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "CampaignCriterionService")
    campaign_rn = _service(client, "CampaignService").campaign_path(
        customer_id, campaign_id
    )

    req = client.get_type("MutateCampaignCriteriaRequest")
    req.customer_id = customer_id
    req.validate_only = dry_run
    req.partial_failure = False

    for text in keywords:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = text.strip()
        crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
        req.operations.append(op)

    try:
        svc.mutate_campaign_criteria(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    detail = {
        "campaign_id": campaign_id,
        "match_type": match_type,
        "count": len(keywords),
        "keywords": keywords,
    }
    _log("add_campaign_negative_keywords", customer_id, dry_run, detail)
    return {"dry_run": dry_run, "applied": not dry_run, **detail}
